"""Shadow-cycle invariants and export activation gate.

Shadow runs execute the complete backlog analysis without exporting implementation
tickets.  A configurable number of consecutive runs must pass the depth invariants
and produce the same source-observation corpus, canonical case graph, and causal plan
intent before export is unlocked. The
runner-owned research proof basis is always part of cross-cycle stability. Full
backlog byte and content hashes always bind the latest qualifying cycle to the current
file; they are not the cross-cycle stability key. The retained full export projection
binds the latest cycle to the exported artifact, but generated prose, fingerprints, and
plan revision IDs do not reset a semantic stability streak.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core import assess_research_readiness, assess_ticket_readiness
from backlog_core.case_lineage import (
    ATOM_DISPOSITIONS,
    atom_disposition_receipt_errors,
    atom_is_idea_originated,
)
from backlog_miner.research_evidence import verify_persisted_research_evidence
from backlog_repo import validate_case_relation_receipt, verify_outcome_record_provenance

from usertest_backlog.workflows.problem_mining_evidence import (
    verify_problem_mining_evidence_receipt,
)

_DERIVED_EVIDENCE_ROLES = frozenset({"research", "implementation", "verification"})
_HIGH_SEVERITIES = frozenset({"high", "blocker"})
_CASE_TERMINAL_OUTCOMES = frozenset({"resolved", "duplicate", "superseded"})
_SHADOW_STATE_SCHEMA_VERSION = 7
_SHADOW_CYCLE_SCHEMA_VERSION = 5
_DEFAULT_REQUIRED_CONSECUTIVE_CYCLES = 2
_DEFAULT_REQUIRE_EXACT_EXPORT_PROJECTION = True
_REQUIRED_SHADOW_ARTIFACTS = frozenset(
    {
        "atoms",
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    }
)
_INVARIANT_HASH_FIELDS = (
    "atom_corpus_sha256",
    "source_atom_corpus_sha256",
    "case_graph_sha256",
    "ticket_set_sha256",
    "research_proof_basis_sha256",
)
_SHADOW_CYCLE_FIELDS = frozenset(
    {
        "cycle_schema_version",
        "cycle_id",
        "run_identity_sha256",
        "generated_at",
        "schema_version",
        "passed",
        "failures",
        "checks",
        *_INVARIANT_HASH_FIELDS,
        "export_projection_sha256",
        "export_inputs_sha256",
        "counts",
        "invariant_report_sha256",
        "backlog_path",
        "backlog_sha256",
        "backlog_content_sha256",
        "backlog_snapshot_path",
        "artifact_receipts",
        "required_consecutive_cycles",
        "require_exact_export_projection",
        "cycle_receipt_path",
        "cycle_receipt_sha256",
    }
)
_SHADOW_STATE_FIELDS = frozenset(
    {
        "schema_version",
        "backlog_path",
        "ready_for_export",
        "required_consecutive_cycles",
        "require_exact_export_projection",
        "consecutive_stable_passes",
        "validated_cycle_id",
        "validated_backlog_sha256",
        "validated_backlog_content_sha256",
        "validated_export_inputs_sha256",
        "validated_research_proof_basis_sha256",
        "cycles",
    }
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _items(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("items")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backlog_content_sha256(path: Path) -> str:
    """Hash the backlog document without its top-level generation timestamp."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("backlog artifact must contain a JSON object")
    projected = dict(raw)
    projected.pop("generated_at_utc", None)
    return _canonical_hash(projected)


def _json_content_sha256(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return _canonical_hash(raw)


def shadow_state_path(backlog_path: Path) -> Path:
    """Return the activation-state path paired with a backlog artifact."""
    stem = backlog_path.stem.removesuffix(".backlog")
    return backlog_path.with_name(f"{stem}.shadow_state.json")


def normalize_shadow_gate_config(raw: object) -> dict[str, Any]:
    """Validate and normalize the repository-owned shadow export-gate config."""

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("backlog_export_gate must be a mapping")

    if "require_exact_backlog_hash" in raw:
        raise ValueError(
            "backlog_export_gate.require_exact_backlog_hash was replaced by "
            "require_exact_export_projection"
        )
    allowed_fields = {
        "enabled",
        "required_consecutive_shadow_cycles",
        "require_exact_export_projection",
    }
    unknown_fields = sorted(set(raw) - allowed_fields)
    if unknown_fields:
        raise ValueError(
            "backlog_export_gate contains unknown fields: " + ", ".join(unknown_fields)
        )

    enabled_raw = raw.get("enabled", False)
    if not isinstance(enabled_raw, bool):
        raise ValueError("backlog_export_gate.enabled must be a boolean")

    required_raw = raw.get(
        "required_consecutive_shadow_cycles",
        _DEFAULT_REQUIRED_CONSECUTIVE_CYCLES,
    )
    if isinstance(required_raw, bool) or not isinstance(required_raw, int) or required_raw < 1:
        raise ValueError(
            "backlog_export_gate.required_consecutive_shadow_cycles must be a positive integer"
        )

    exact_raw = raw.get(
        "require_exact_export_projection",
        _DEFAULT_REQUIRE_EXACT_EXPORT_PROJECTION,
    )
    if not isinstance(exact_raw, bool):
        raise ValueError("backlog_export_gate.require_exact_export_projection must be a boolean")

    return {
        "enabled": enabled_raw,
        "required_consecutive_shadow_cycles": required_raw,
        "require_exact_export_projection": exact_raw,
    }


def _case_graph_projection(
    case_registry: dict[str, Any],
    *,
    source_atom_ids: set[str] | None = None,
) -> dict[str, Any]:
    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    projected: dict[str, Any] = {}
    for case_id, raw_case in sorted(cases.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_case, dict):
            continue
        projected[str(case_id)] = {
            "state": _text(raw_case.get("state")) or "active",
            "evidence_atom_ids": sorted(
                {
                    str(value)
                    for field in ("evidence_atom_ids", "supporting_atom_ids")
                    for value in (
                        raw_case.get(field, []) if isinstance(raw_case.get(field), list) else []
                    )
                    if isinstance(value, str)
                    and value.strip()
                    and (source_atom_ids is None or value in source_atom_ids)
                }
            ),
            "split_from_case_id": _text(raw_case.get("split_from_case_id")),
            "same_cause_group_id": _text(raw_case.get("same_cause_group_id")),
        }
    return projected


def _ticket_projection(
    backlog: dict[str, Any],
    *,
    source_atom_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Project canonical case/plan intent, excluding generated prose identities.

    The latest backlog and export projection remain byte/content-addressed elsewhere.
    This projection answers the narrower cross-cycle question: did the pipeline choose
    the same causal mechanism, intervention boundary, change surface, and proof oracle?
    """

    def records(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        projected = [
            {field: item.get(field) for field in fields}
            for item in value
            if isinstance(item, Mapping)
        ]
        return sorted(projected, key=_canonical_hash)

    def coverage_projection(value: Any) -> dict[str, Any] | None:
        coverage = dict(value) if isinstance(value, Mapping) else {}
        binding_raw = coverage.get("research_binding")
        binding = dict(binding_raw) if isinstance(binding_raw, Mapping) else {}
        if not binding:
            return None
        interventions: list[dict[str, Any]] = []
        for raw_point in binding.get("intervention_points", []):
            if not isinstance(raw_point, Mapping):
                continue
            point = dict(raw_point)
            interventions.append(
                {
                    "mechanism_symbol": _text(point.get("mechanism_symbol")),
                    "controls_mechanism_symbols": _sorted_strings(
                        point.get("controls_mechanism_symbols")
                    ),
                    "causal_role": _text(point.get("causal_role")),
                    "target_path": _text(point.get("target_path")),
                    "target_symbol": _text(point.get("target_symbol")),
                }
            )
        return {
            "hypothesis_id": _text(binding.get("hypothesis_id")),
            "mechanism_symbols": _sorted_strings(binding.get("mechanism_symbols")),
            "supporting_evidence_refs": _sorted_strings(binding.get("supporting_evidence_refs")),
            "counterevidence_refs": _sorted_strings(binding.get("counterevidence_refs")),
            "falsification_attempt_refs": _sorted_strings(
                binding.get("falsification_attempt_refs")
            ),
            "intervention_points": sorted(interventions, key=_canonical_hash),
        }

    def scope_projection(value: Any) -> dict[str, Any] | None:
        scope = dict(value) if isinstance(value, Mapping) else {}
        if not scope:
            return None
        paths = [
            {
                "evidence_refs": _sorted_strings(item.get("evidence_refs")),
            }
            for item in scope.get("independent_consumers_or_failure_paths", [])
            if isinstance(item, Mapping)
        ]
        return {
            "scope_level": _text(scope.get("scope_level")),
            "paths": sorted(paths, key=_canonical_hash),
        }

    def plan_projection(ticket: Mapping[str, Any]) -> dict[str, Any] | None:
        raw_plan = ticket.get("change_plan")
        plan = dict(raw_plan) if isinstance(raw_plan, Mapping) else {}
        raw_selection = ticket.get("selected_solution")
        selection = dict(raw_selection) if isinstance(raw_selection, Mapping) else {}
        coverage = plan.get("causal_coverage", selection.get("causal_coverage"))
        scope = plan.get("scope_evidence", selection.get("scope_evidence"))
        if not plan and not coverage:
            return None
        targets: list[dict[str, Any]] = []
        for raw_target in plan.get("change_targets", []):
            if not isinstance(raw_target, Mapping):
                continue
            target = dict(raw_target)
            raw_integration = target.get("integration_binding")
            integration = dict(raw_integration) if isinstance(raw_integration, Mapping) else {}
            targets.append(
                {
                    "action": _text(target.get("action")),
                    "path": _text(target.get("path")),
                    "symbols": _sorted_strings(target.get("symbols")),
                    "rationale_kind": _text(target.get("rationale_kind")),
                    "evidence_refs": _sorted_strings(target.get("evidence_refs")),
                    "integration_binding": (
                        {
                            "path": _text(integration.get("path")),
                            "symbol": _text(integration.get("symbol")),
                            "evidence_refs": _sorted_strings(integration.get("evidence_refs")),
                        }
                        if integration
                        else None
                    ),
                }
            )
        reproduction_raw = plan.get("before_after_reproduction")
        reproduction = dict(reproduction_raw) if isinstance(reproduction_raw, Mapping) else {}
        before_raw = reproduction.get("before_change")
        before = dict(before_raw) if isinstance(before_raw, Mapping) else {}
        after_raw = reproduction.get("after_change")
        after = dict(after_raw) if isinstance(after_raw, Mapping) else {}
        roles_raw = plan.get("outcome_verification_roles")
        roles = dict(roles_raw) if isinstance(roles_raw, Mapping) else {}
        role_projection: dict[str, Any] = {}
        for role_name, raw_role in sorted(roles.items(), key=lambda item: str(item[0])):
            role = dict(raw_role) if isinstance(raw_role, Mapping) else {}
            role_projection[str(role_name)] = (
                {
                    "research_experiment_id": _text(role.get("research_experiment_id")),
                    "commands": list(role.get("commands", []))
                    if isinstance(role.get("commands"), list)
                    else [],
                    "predicates": records(
                        role.get("predicates"),
                        (
                            "type",
                            "command_index",
                            "equals",
                            "value",
                            "path",
                            "pointer",
                        ),
                    ),
                }
                if role
                else None
            )
        return {
            "causal_binding": coverage_projection(coverage),
            "scope": scope_projection(scope),
            "change_targets": sorted(targets, key=_canonical_hash),
            "before_after": {
                "research_experiment_id": _text(reproduction.get("research_experiment_id")),
                "expected_outcome_state": _text(reproduction.get("expected_outcome_state")),
                "before_command": _text(before.get("command")),
                "before_exit": before.get("expected_exit_code"),
                "before_assertion": (
                    dict(before["observable_assertion"])
                    if isinstance(before.get("observable_assertion"), Mapping)
                    else None
                ),
                "after_command": _text(after.get("command")),
                "after_exit": after.get("expected_exit_code"),
                "after_assertions": records(
                    after.get("observable_assertions"),
                    ("source", "operator", "expected", "path", "pointer"),
                ),
            },
            "outcome_roles": role_projection,
            "requires_live_verification": plan.get("requires_live_verification"),
        }

    raw = backlog.get("tickets")
    tickets = raw if isinstance(raw, list) else []
    return sorted(
        [
            {
                "case_id": _text(ticket.get("case_id")),
                "stage": _text(ticket.get("stage")),
                "evidence_atom_ids": sorted(
                    value
                    for value in ticket.get("evidence_atom_ids", [])
                    if isinstance(value, str)
                    and value.strip()
                    and (source_atom_ids is None or value in source_atom_ids)
                ),
                "plan_intent": plan_projection(ticket),
            }
            for ticket in tickets
            if isinstance(ticket, dict)
        ],
        key=lambda item: (
            str(item.get("case_id")),
            str(item.get("stage")),
            _canonical_hash(item),
        ),
    )


def _relation_decisions(
    stage1: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    meta_raw = stage1.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    expected_count = meta.get("relation_review_decision_count")
    if not isinstance(expected_count, int) or expected_count == 0:
        return [], None, []
    artifacts_raw = stage1.get("artifacts")
    artifacts = artifacts_raw if isinstance(artifacts_raw, dict) else {}
    receipt_path_raw = _text(artifacts.get("relation_review_receipt"))
    if receipt_path_raw is None:
        return [], None, ["relation_review_receipt_missing"]
    receipt_path = Path(receipt_path_raw).expanduser().resolve()
    try:
        receipt_raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return [], None, [f"relation_review_receipt_unreadable:{type(exc).__name__}"]
    if not isinstance(receipt_raw, Mapping):
        return [], None, ["relation_review_receipt_invalid"]
    try:
        receipt = validate_case_relation_receipt(receipt_raw)
    except ValueError as exc:
        return [], None, [f"relation_review_receipt_invalid:{exc}"]
    if receipt.get("stage") != "problem_mining":
        return [], None, ["relation_review_receipt_stage_invalid"]
    content_hash = str(receipt["content_sha256"])
    if not receipt_path.name.endswith(f".{content_hash[:16]}{receipt_path.suffix}"):
        return [], None, ["relation_review_receipt_path_not_content_addressed"]

    response_path = Path(str(receipt["relation_review_response_path"])).expanduser().resolve()
    response_hash = str(receipt["relation_review_response_sha256"])
    if response_path.parent != receipt_path.parent:
        return [], None, ["relation_review_response_snapshot_not_sibling"]
    if not response_path.name.endswith(f".snapshot-{response_hash[:16]}{response_path.suffix}"):
        return [], None, ["relation_review_response_snapshot_not_content_addressed"]
    try:
        response_bytes = response_path.read_bytes()
    except OSError as exc:
        return [], None, [f"relation_review_response_snapshot_unreadable:{type(exc).__name__}"]
    if sha256(response_bytes).hexdigest() != response_hash:
        return [], None, ["relation_review_response_snapshot_hash_mismatch"]
    try:
        parsed = json.loads(response_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return [], None, ["relation_review_response_snapshot_invalid"]
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        return [], None, ["relation_review_response_snapshot_invalid"]
    decisions = [dict(item) for item in parsed]
    if len(decisions) != expected_count:
        return decisions, receipt, ["relation_review_decision_count_mismatch"]
    return decisions, receipt, []


def _relation_application_errors(stage1: dict[str, Any]) -> list[str]:
    records = _items(stage1)
    decisions, receipt, errors = _relation_decisions(stage1)
    membership: dict[str, str] = {}
    split_children: dict[str, list[tuple[str, ...]]] = {}
    for record in records:
        case_id = _text(record.get("case_id")) or _text(record.get("problem_id"))
        if case_id is None:
            continue
        members = {
            value
            for value in [
                _text(record.get("problem_id")),
                *[
                    _text(value)
                    for value in (
                        record.get("case_member_problem_ids", [])
                        if isinstance(record.get("case_member_problem_ids"), list)
                        else []
                    )
                ],
            ]
            if value is not None
        }
        for member in members:
            membership[member] = case_id
        evidence_group = tuple(
            sorted(
                value
                for value in record.get("evidence_atom_ids", [])
                if isinstance(value, str) and value.strip()
            )
        )
        for parent_problem_id in (
            record.get("split_parent_problem_ids", [])
            if isinstance(record.get("split_parent_problem_ids"), list)
            else []
        ):
            if isinstance(parent_problem_id, str) and parent_problem_id.strip():
                split_children.setdefault(parent_problem_id, []).append(evidence_group)

    expected_receipt_edges: list[dict[str, Any]] = []
    for record in records:
        target_case_id = _text(record.get("case_id"))
        absorbed_case_ids = (
            sorted(
                {
                    value.strip()
                    for value in record.get("absorbed_case_ids", [])
                    if isinstance(value, str) and value.strip()
                }
            )
            if isinstance(record.get("absorbed_case_ids"), list)
            else []
        )
        actions = (
            sorted(
                {
                    action
                    for raw_action in record.get("case_relation_actions", [])
                    if isinstance(raw_action, Mapping)
                    for action in [_text(raw_action.get("action"))]
                    if action in {"merge", "alias", "same_cause_group"}
                }
            )
            if isinstance(record.get("case_relation_actions"), list)
            else []
        )
        if target_case_id is None:
            continue
        expected_receipt_edges.extend(
            {
                "source_case_id": source_case_id,
                "target_case_id": target_case_id,
                "direction": "source_to_canonical",
                "relation_kind": "canonical_absorption",
                "decision_actions": actions,
            }
            for source_case_id in absorbed_case_ids
        )
    if receipt is not None:
        actual_receipt_edges = [
            {
                key: relation.get(key)
                for key in (
                    "source_case_id",
                    "target_case_id",
                    "direction",
                    "relation_kind",
                    "decision_actions",
                )
            }
            for relation in receipt.get("relations", [])
            if isinstance(relation, Mapping)
        ]
        if sorted(expected_receipt_edges, key=_canonical_hash) != sorted(
            actual_receipt_edges,
            key=_canonical_hash,
        ):
            errors.append("relation_review_receipt_edges_not_applied")

    for index, decision in enumerate(decisions):
        action = _text(decision.get("action"))
        focus = _text(decision.get("focus_id"))
        if action == "merge":
            raw_members = decision.get("target_ids", [])
        elif action == "alias":
            raw_members = [decision.get("alias_target_id")]
        elif action == "same_cause_group":
            raw_members = decision.get("member_ids", [])
        elif action == "keep_separate":
            raw_members = decision.get("target_ids", [])
        else:
            raw_members = []
        members = [focus] if focus is not None else []
        members.extend(
            value
            for value in (
                _text(raw_value)
                for raw_value in (raw_members if isinstance(raw_members, list) else [])
            )
            if value is not None
        )
        member_cases = {membership.get(member) for member in members}
        if action in {"merge", "alias", "same_cause_group"}:
            if None in member_cases or len(member_cases) != 1:
                errors.append(f"relation_decision_not_applied:{index}:{action}")
        elif action == "keep_separate" and len(members) >= 2:
            if None in member_cases or len(member_cases) != len(set(members)):
                errors.append(f"relation_keep_separate_not_applied:{index}")
        elif action == "split" and focus is not None:
            groups_raw = decision.get("split_groups")
            expected_groups = sorted(
                tuple(
                    sorted(
                        value
                        for value in group.get("evidence_atom_ids", [])
                        if isinstance(value, str) and value.strip()
                    )
                )
                for group in (groups_raw if isinstance(groups_raw, list) else [])
                if isinstance(group, dict)
            )
            actual_groups = sorted(split_children.get(focus, []))
            if not expected_groups or actual_groups != expected_groups:
                errors.append(f"relation_split_not_applied:{index}")
    return errors


def _terminal_outcome_errors(
    case_registry: dict[str, Any],
    *,
    trusted_runs_roots: tuple[Path, ...],
    owner_roots: tuple[Path, ...],
) -> list[str]:
    errors: list[str] = []
    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    for case_id, raw_case in cases.items():
        if not isinstance(raw_case, dict):
            continue
        state = _text(raw_case.get("state")) or "active"
        plan_outcomes_raw = raw_case.get("plan_outcomes")
        plan_outcomes = plan_outcomes_raw if isinstance(plan_outcomes_raw, dict) else {}
        valid_records: list[dict[str, Any]] = []
        for plan_revision_id, projection in plan_outcomes.items():
            if not isinstance(projection, dict):
                continue
            verification_raw = projection.get("outcome_verification")
            if (
                isinstance(verification_raw, dict)
                and verification_raw.get("structural_status") == "valid"
                and verification_raw.get("verified") is not True
            ):
                errors.append(
                    "outcome_provenance_revalidation_failed:"
                    f"{case_id}:{plan_revision_id}:"
                    + ",".join(
                        str(error)
                        for error in verification_raw.get("errors", [])
                        if isinstance(error, str)
                    )
                )
            outcome_raw = projection.get("outcome_record")
            if not isinstance(outcome_raw, dict):
                continue
            verification = verify_outcome_record_provenance(
                outcome_raw,
                trusted_runs_roots=trusted_runs_roots,
                owner_roots=owner_roots,
                case_registry=case_registry,
            )
            outcome = verification.get("outcome_record")
            if verification.get("verified") is not True or not isinstance(outcome, dict):
                errors.append(
                    "outcome_provenance_revalidation_failed:"
                    f"{case_id}:{plan_revision_id}:"
                    + ",".join(str(error) for error in verification.get("errors", []))
                )
                continue
            if outcome.get("case_id") == case_id and outcome.get("state") == state:
                valid_records.append(outcome)
        if state in _CASE_TERMINAL_OUTCOMES and not valid_records:
            errors.append(f"terminal_case_missing_validated_outcome:{case_id}:{state}")
    return errors


def _problem_to_case_ids(stage1: dict[str, Any], case_registry: dict[str, Any]) -> dict[str, str]:
    """Return the canonical problem-to-case aliases used by every stage."""

    aliases_raw = case_registry.get("problem_id_to_case_id")
    aliases = aliases_raw if isinstance(aliases_raw, dict) else {}
    result = {
        str(problem_id): str(case_id)
        for problem_id, case_id in aliases.items()
        if _text(problem_id) is not None and _text(case_id) is not None
    }
    for record in _items(stage1):
        case_id = _text(record.get("case_id"))
        if case_id is None:
            continue
        problem_ids = [
            _text(record.get("problem_id")),
            *[
                _text(value)
                for value in (
                    record.get("case_member_problem_ids", [])
                    if isinstance(record.get("case_member_problem_ids"), list)
                    else []
                )
            ],
        ]
        for problem_id in problem_ids:
            if problem_id is not None:
                result[problem_id] = case_id
    return result


def _record_case_id(record: dict[str, Any], aliases: Mapping[str, str]) -> str | None:
    return _text(record.get("case_id")) or aliases.get(_text(record.get("problem_id")) or "")


def _records_by_case(
    records: list[dict[str, Any]], aliases: Mapping[str, str]
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        case_id = _record_case_id(record, aliases)
        if case_id is not None:
            result.setdefault(case_id, []).append(record)
    return result


def _stage_meta_records(document: dict[str, Any], field: str) -> list[dict[str, Any]]:
    meta_raw = document.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    raw = meta.get(field)
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _has_nonempty_reasons(record: dict[str, Any]) -> bool:
    if _text(record.get("decision_rationale")) is not None:
        return True
    reasons = record.get("reasons")
    return bool(isinstance(reasons, list) and any(_text(reason) is not None for reason in reasons))


def _has_explicit_research_block(record: dict[str, Any]) -> bool:
    if _text(record.get("research_status")) not in {"insufficient_evidence", "blocked"}:
        return False
    return any(
        isinstance(record.get(field), list) and bool(record.get(field))
        for field in ("blocking_reasons", "material_unknowns", "evidence_boundaries")
    )


def _is_meaningful_automated_observation(atom: Mapping[str, Any]) -> bool:
    """Exclude external ideas, derived commentary, and proposal-only evidence."""

    if atom_is_idea_originated(atom):
        return False
    if _text(atom.get("evidence_role")) in _DERIVED_EVIDENCE_ROLES:
        return False
    return (_text(atom.get("evidence_class")) or "observation").casefold() != "proposal"


def _unresolved_high_severity_work_errors(
    *,
    atoms: list[dict[str, Any]],
    backlog: dict[str, Any],
    stage1: dict[str, Any],
    stage2: dict[str, Any],
    stage3: dict[str, Any],
    case_registry: dict[str, Any],
) -> list[str]:
    """Require unresolved severe observations to remain visible as durable work.

    A rationale on the atom proves that a decision was made, but does not prove the
    observation will be researched again. Qualification therefore requires an active
    canonical case, selection into research, and either a retained research result
    (including an honest evidence block) or a non-terminal backlog work item.
    """

    aliases = _problem_to_case_ids(stage1, case_registry)
    stage3_meta_raw = stage3.get("input_meta")
    stage3_meta = stage3_meta_raw if isinstance(stage3_meta_raw, dict) else {}
    post_aliases_raw = stage3_meta.get("post_research_case_aliases")
    post_aliases = (
        {
            str(source): str(target)
            for source, target in post_aliases_raw.items()
            if _text(source) is not None and _text(target) is not None and source != target
        }
        if isinstance(post_aliases_raw, dict)
        else {}
    )

    def canonical_case_id(case_id: str) -> str:
        seen: set[str] = set()
        while case_id in post_aliases and case_id not in seen:
            seen.add(case_id)
            case_id = post_aliases[case_id]
        return case_id

    stage1_cases_by_atom: dict[str, set[str]] = {}
    for record in _items(stage1):
        case_id = _record_case_id(record, aliases)
        if case_id is None:
            continue
        case_id = canonical_case_id(case_id)
        for field in (
            "evidence_atom_ids",
            "source_evidence_atom_ids",
            "supporting_atom_ids",
        ):
            raw_atom_ids = record.get(field)
            if not isinstance(raw_atom_ids, list):
                continue
            for atom_id in raw_atom_ids:
                if isinstance(atom_id, str) and atom_id.strip():
                    stage1_cases_by_atom.setdefault(atom_id.strip(), set()).add(case_id)

    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    active_cases = {
        canonical_case_id(str(case_id))
        for case_id, raw_case in cases.items()
        if isinstance(raw_case, dict)
        and (_text(raw_case.get("state")) or "active") not in _CASE_TERMINAL_OUTCOMES
        and not any(
            _text(raw_case.get(field)) is not None
            for field in ("alias_of", "duplicate_of", "superseded_by")
        )
    }
    selected_cases = {
        canonical_case_id(case_id)
        for record in _items(stage2)
        for case_id in [_record_case_id(record, aliases)]
        if case_id is not None and record.get("selected_for_research") is True
    }
    retained_research_cases = {
        canonical_case_id(case_id)
        for record in _items(stage3)
        for case_id in [_record_case_id(record, aliases)]
        if case_id is not None
        and (
            _text(record.get("research_status")) == "evidence_sufficient"
            or _has_explicit_research_block(record)
        )
    }
    tickets_raw = backlog.get("tickets")
    tickets = (
        [ticket for ticket in tickets_raw if isinstance(ticket, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    retained_backlog_cases = {
        canonical_case_id(case_id)
        for ticket in tickets
        for case_id in [_record_case_id(ticket, aliases)]
        if case_id is not None
        and (_text(ticket.get("stage")) or "")
        in {"triage", "research_required", "blocked", "ready_for_ticket"}
    }
    durable_cases = (
        active_cases & selected_cases & (retained_research_cases | retained_backlog_cases)
    )

    errors: list[str] = []
    for atom in atoms:
        atom_id = _text(atom.get("atom_id")) or "(missing)"
        if (
            _is_meaningful_automated_observation(atom)
            and (_text(atom.get("severity_hint")) or "").casefold() in _HIGH_SEVERITIES
            and _text(atom.get("disposition")) == "unresolved"
            and _text(atom.get("disposition_status")) == "decided"
            and not stage1_cases_by_atom.get(atom_id, set()).intersection(durable_cases)
        ):
            errors.append(f"high_severity_unresolved_without_active_work:{atom_id}")
    return errors


def _case_conservation_errors(
    *,
    backlog: dict[str, Any],
    stage1: dict[str, Any],
    stage2: dict[str, Any],
    stage3: dict[str, Any],
    stage4: dict[str, Any],
    stage5: dict[str, Any],
    stage6: dict[str, Any],
    case_registry: dict[str, Any],
) -> list[str]:
    """Prove that every canonical stage-1 case reaches a durable disposition.

    A case must enter research, may then stop at an explicit evidence/option/plan block,
    or stop at a validated terminal lifecycle outcome. Merely omitting or indefinitely
    priority-deferring the case is never a disposition.
    """

    failures: list[str] = []
    aliases = _problem_to_case_ids(stage1, case_registry)
    stage3_meta_raw = stage3.get("input_meta")
    stage3_meta = stage3_meta_raw if isinstance(stage3_meta_raw, dict) else {}
    post_aliases_raw = stage3_meta.get("post_research_case_aliases")
    post_aliases = (
        {
            str(source): str(target)
            for source, target in post_aliases_raw.items()
            if _text(source) is not None and _text(target) is not None and source != target
        }
        if isinstance(post_aliases_raw, dict)
        else {}
    )

    def canonical_case_id(case_id: str) -> str:
        seen: set[str] = set()
        while case_id in post_aliases and case_id not in seen:
            seen.add(case_id)
            case_id = post_aliases[case_id]
        return case_id

    stage1_cases: set[str] = set()
    for record in _items(stage1):
        case_id = _record_case_id(record, aliases)
        if case_id is None:
            failures.append(
                "case_conservation_stage1_identity_missing:"
                f"{_text(record.get('problem_id')) or '(missing)'}"
            )
        else:
            stage1_cases.add(canonical_case_id(case_id))

    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, dict) else {}
    terminal_cases = {
        str(case_id)
        for case_id, raw_case in cases.items()
        if isinstance(raw_case, dict)
        and (_text(raw_case.get("state")) or "active") in _CASE_TERMINAL_OUTCOMES
    }

    def canonical_records_by_case(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped = _records_by_case(records, aliases)
        result: dict[str, list[dict[str, Any]]] = {}
        for case_id, values in grouped.items():
            result.setdefault(canonical_case_id(case_id), []).extend(values)
        return result

    priority_by_case = canonical_records_by_case(_items(stage2))
    research_by_case = canonical_records_by_case(_items(stage3))
    options_by_case = canonical_records_by_case(_items(stage4))
    option_outcomes_by_case = canonical_records_by_case(
        _stage_meta_records(stage4, "optioning_outcomes")
    )
    selections_by_case = canonical_records_by_case(_items(stage5))
    selection_outcomes_by_case = canonical_records_by_case(
        _stage_meta_records(stage5, "selection_outcomes")
    )
    plans_by_case = canonical_records_by_case(_items(stage6))
    rejected_plans_by_case = canonical_records_by_case(
        _stage_meta_records(stage6, "rejected_plans")
    )
    tickets_raw = backlog.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    tickets_by_case = canonical_records_by_case(tickets)

    def missing(case_id: str, source: str, destination: str) -> None:
        failures.append(f"case_conservation_missing:{case_id}:{source}->{destination}")

    for case_id in sorted(stage1_cases):
        if case_id in terminal_cases:
            continue

        priorities = priority_by_case.get(case_id, [])
        if not priorities:
            missing(case_id, "problem_mining", "problem_prioritization")
            continue
        if not any(item.get("selected_for_research") is True for item in priorities):
            failures.append(f"case_conservation_priority_disposition_invalid:{case_id}")
            continue

        research = research_by_case.get(case_id, [])
        if not research:
            missing(case_id, "problem_prioritization", "repro_research")
            continue
        if not any(
            _text(item.get("research_status")) == "evidence_sufficient" for item in research
        ):
            if any(_has_explicit_research_block(item) for item in research):
                continue
            failures.append(f"case_conservation_research_disposition_invalid:{case_id}")
            continue

        options = options_by_case.get(case_id, [])
        if not options:
            option_outcomes = option_outcomes_by_case.get(case_id, [])
            if any(
                _text(item.get("optioning_status"))
                in {"insufficient_evidence", "no_safe_option", "invalid_output"}
                and _has_nonempty_reasons(item)
                for item in option_outcomes
            ):
                continue
            missing(case_id, "repro_research", "solution_optioning")
            continue

        selections = selections_by_case.get(case_id, [])
        if not selections:
            selection_outcomes = selection_outcomes_by_case.get(case_id, [])
            if any(
                _text(item.get("selection_status"))
                in {"insufficient_evidence", "invalid_output", "reject"}
                and _has_nonempty_reasons(item)
                for item in selection_outcomes
            ):
                continue
            missing(case_id, "solution_optioning", "solution_selection")
            continue

        plans = plans_by_case.get(case_id, [])
        if not plans:
            rejected = rejected_plans_by_case.get(case_id, [])
            if any(
                _text(item.get("planning_status")) in {"blocked", "invalid_output"}
                and _has_nonempty_reasons(item)
                for item in rejected
            ):
                continue
            missing(case_id, "solution_selection", "implementation_planning")
            continue

        if not tickets_by_case.get(case_id):
            missing(case_id, "implementation_planning", "backlog")

    return failures


def _atom_corpus_projection(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return canonical atom evidence, including content, severity, and lineage.

    Hashing IDs alone allowed the meaning and severity of a retained observation to
    change without resetting a shadow streak. The complete normalized atom mapping is
    decision evidence; JSON object ordering is canonicalized by ``_canonical_hash``.
    """

    return sorted(
        [dict(atom) for atom in atoms],
        key=lambda atom: (
            _text(atom.get("atom_id")) or "",
            _canonical_hash(atom),
        ),
    )


def _sorted_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})


def _sorted_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records = [dict(item) for item in value if isinstance(item, Mapping)]
    return sorted(records, key=_canonical_hash)


def _origin_assignment_proof_basis(value: Any) -> dict[str, Any]:
    assignment = dict(value) if isinstance(value, Mapping) else {}
    atom_receipts: list[dict[str, Any]] = []
    for raw_receipt in assignment.get("atom_receipts", []):
        if not isinstance(raw_receipt, Mapping):
            continue
        receipt = dict(raw_receipt)
        artifact_receipts: list[dict[str, Any]] = []
        for raw_artifact in receipt.get("artifact_receipts", []):
            if not isinstance(raw_artifact, Mapping):
                continue
            artifact = dict(raw_artifact)
            path = _text(artifact.get("path"))
            artifact_receipts.append(
                {
                    "path": path.replace("\\", "/") if path is not None else None,
                    "sha256": artifact.get("sha256"),
                    "size_bytes": artifact.get("size_bytes"),
                }
            )
        atom_receipts.append(
            {
                "atom_id": _text(receipt.get("atom_id")),
                "atom_sha256": receipt.get("atom_sha256"),
                "artifact_receipts": sorted(artifact_receipts, key=_canonical_hash),
            }
        )
    return {
        "status": assignment.get("status"),
        "case_id": _text(assignment.get("case_id")),
        "problem_id": _text(assignment.get("problem_id")),
        "expected_atom_ids": _sorted_strings(assignment.get("expected_atom_ids")),
        "assignment_sha256": assignment.get("assignment_sha256"),
        "atom_receipts": sorted(atom_receipts, key=_canonical_hash),
    }


def _replay_isolation_proof_basis(value: Any) -> dict[str, Any]:
    isolation = dict(value) if isinstance(value, Mapping) else {}
    return {
        "executor": _text(isolation.get("executor")),
        "os_sandbox": isolation.get("os_sandbox"),
        "network": isolation.get("network"),
        "filesystem_isolation": _text(isolation.get("filesystem_isolation")),
        "trust_decision": _text(isolation.get("trust_decision")),
        "sanitized_environment_keys": _sorted_strings(isolation.get("sanitized_environment_keys")),
    }


def _workspace_overlay_proof_basis(value: Any) -> dict[str, Any]:
    overlay = dict(value) if isinstance(value, Mapping) else {}
    return {
        field: overlay.get(field)
        for field in (
            "baseline_manifest_sha256",
            "research_manifest_sha256",
            "baseline_state_sha256",
            "research_state_sha256",
            "baseline_git_index_sha256",
            "research_git_index_sha256",
            "research_overlay_manifest_sha256",
            "git_index_changed",
        )
    } | {
        "changed_baseline_paths": _sorted_strings(overlay.get("changed_baseline_paths")),
        "research_overlay_paths": _sorted_strings(overlay.get("research_overlay_paths")),
        "suspicious_extra_paths": _sorted_strings(overlay.get("suspicious_extra_paths")),
        "research_overlay_manifest": (
            dict(overlay["research_overlay_manifest"])
            if isinstance(overlay.get("research_overlay_manifest"), Mapping)
            else {}
        ),
    }


def _stable_research_path(value: Any, *, receipt: Mapping[str, Any]) -> str | None:
    path = _text(value)
    if path is None:
        return None
    normalized = path.replace("\\", "/")
    for label, field in (
        ("$research_workspace", "workspace_dir"),
        ("$research_run", "run_dir"),
    ):
        root = _text(receipt.get(field))
        if root is None:
            continue
        normalized_root = root.replace("\\", "/").rstrip("/")
        if normalized.casefold() == normalized_root.casefold():
            return label
        prefix = normalized_root + "/"
        if normalized.casefold().startswith(prefix.casefold()):
            return f"{label}/{normalized[len(prefix) :]}"
    return normalized


def _artifact_reference_proof_basis(
    value: Any,
    *,
    aliases: Mapping[str, str],
) -> str | None:
    reference = _text(value)
    if reference is None:
        return None
    normalized = reference.replace("\\", "/")
    return aliases.get(normalized.casefold(), normalized)


def _experiment_proof_basis(
    value: Mapping[str, Any],
    *,
    case_id: str,
    replay_executor: str | None,
    artifact_aliases: Mapping[str, str],
) -> tuple[dict[str, Any], list[str]]:
    experiment = dict(value)
    isolation = _replay_isolation_proof_basis(experiment.get("execution_isolation"))
    metadata_raw = experiment.get("execution_metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
    experiment_id = _text(experiment.get("experiment_id")) or "(missing)"
    errors: list[str] = []
    executor = _text(metadata.get("executor"))
    if replay_executor is not None and executor != replay_executor:
        errors.append(f"research_proof_basis_executor_mismatch:{case_id}:{experiment_id}")
    image_id = _text(metadata.get("image_id"))
    if executor == "docker" and not (
        isinstance(image_id, str)
        and len(image_id) == 71
        and image_id.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in image_id[7:].casefold())
    ):
        errors.append(f"research_proof_basis_docker_image_unresolved:{case_id}:{experiment_id}")
    execution_metadata = {
        "executor": executor,
        "backend": _text(metadata.get("backend")),
        "image_hash": metadata.get("image_hash"),
        "image_id": image_id,
        "network": metadata.get("network"),
        "os_sandbox": metadata.get("os_sandbox"),
        "cleanup_attempted": metadata.get("cleanup_attempted"),
        "cleanup_confirmed": metadata.get("cleanup_confirmed"),
    }
    projection = {
        "experiment_id": _text(experiment.get("experiment_id")),
        "command": _text(experiment.get("command")),
        "executed_argv": list(experiment.get("executed_argv", []))
        if isinstance(experiment.get("executed_argv"), list)
        else [],
        "exit_code": experiment.get("exit_code"),
        "scenario_kind": _text(experiment.get("scenario_kind")),
        "addresses_atom_ids": _sorted_strings(experiment.get("addresses_atom_ids")),
        "outcome": _text(experiment.get("outcome")),
        "workspace_head": _text(experiment.get("workspace_head")),
        "baseline_state_sha256": experiment.get("baseline_state_sha256"),
        "pre_replay_state_sha256": experiment.get("pre_replay_state_sha256"),
        "post_replay_state_sha256": experiment.get("post_replay_state_sha256"),
        "post_replay_mutations": experiment.get("post_replay_mutations"),
        "overlay_manifest_sha256": experiment.get("overlay_manifest_sha256"),
        "execution_isolation": isolation,
        "execution_metadata": execution_metadata,
        "stdout_sha256": experiment.get("stdout_sha256"),
        "stderr_sha256": experiment.get("stderr_sha256"),
        "observable_assertion": (
            dict(experiment["observable_assertion"])
            if isinstance(experiment.get("observable_assertion"), Mapping)
            else None
        ),
        "assertion_passed": experiment.get("assertion_passed"),
        "artifact_refs": sorted(
            {
                reference
                for item in (
                    experiment.get("artifact_refs", [])
                    if isinstance(experiment.get("artifact_refs"), list)
                    else []
                )
                for reference in [_artifact_reference_proof_basis(item, aliases=artifact_aliases)]
                if reference is not None
            }
        ),
    }
    return projection, errors


def _hypothesis_receipt_proof_basis(
    value: Mapping[str, Any],
    *,
    artifact_aliases: Mapping[str, str],
) -> dict[str, Any]:
    hypothesis = dict(value)
    controls: list[dict[str, Any]] = []
    for raw_control in hypothesis.get("control_links", []):
        if not isinstance(raw_control, Mapping):
            continue
        control = dict(raw_control)
        controls.append(
            {
                "control_experiment_id": _text(control.get("control_experiment_id")),
                "supports_experiment_id": _text(control.get("supports_experiment_id")),
                "mechanism_symbols": _sorted_strings(control.get("mechanism_symbols")),
                "shared_atom_ids": _sorted_strings(control.get("shared_atom_ids")),
                "shared_artifact_refs": sorted(
                    {
                        reference
                        for item in (
                            control.get("shared_artifact_refs", [])
                            if isinstance(control.get("shared_artifact_refs"), list)
                            else []
                        )
                        for reference in [
                            _artifact_reference_proof_basis(
                                item,
                                aliases=artifact_aliases,
                            )
                        ]
                        if reference is not None
                    }
                ),
                "controlled_variable": _text(control.get("controlled_variable")),
                "expected_difference": _text(control.get("expected_difference")),
            }
        )
    return {
        "hypothesis_id": _text(hypothesis.get("hypothesis_id")),
        "supporting_refs": _sorted_strings(hypothesis.get("supporting_refs")),
        "counterevidence_refs": _sorted_strings(hypothesis.get("counterevidence_refs")),
        "mechanism_symbols": _sorted_strings(hypothesis.get("mechanism_symbols")),
        "control_links": sorted(controls, key=_canonical_hash),
    }


def _research_proof_basis_projection(
    dossiers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Project stable, runner-owned evidence behind implementation-ready research.

    Run-local directories, event positions, container names, and mutable image tags are
    deliberately excluded. Their decision-bearing receipts and content hashes remain,
    and Docker replay is bound to the immutable image ID observed by the runner.
    """

    projected: list[dict[str, Any]] = []
    errors: list[str] = []
    for dossier in dossiers:
        receipt_raw = dossier.get("evidence_verification")
        receipt = dict(receipt_raw) if isinstance(receipt_raw, Mapping) else {}
        case_id = _text(dossier.get("case_id")) or "(missing)"
        replay_isolation = _replay_isolation_proof_basis(receipt.get("replay_isolation"))
        replay_executor = _text(replay_isolation.get("executor"))
        artifacts: list[dict[str, Any]] = []
        artifact_aliases: dict[str, str] = {}
        for raw_artifact in receipt.get("artifacts", []):
            if not isinstance(raw_artifact, Mapping):
                continue
            artifact = dict(raw_artifact)
            artifact_id = _text(artifact.get("artifact_id"))
            for alias in (
                artifact_id,
                _text(artifact.get("declared_path")),
                _text(artifact.get("path")),
            ):
                if alias is not None and artifact_id is not None:
                    artifact_aliases[alias.replace("\\", "/").casefold()] = artifact_id
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "kind": _text(artifact.get("kind")),
                    "declared_path": _stable_research_path(
                        artifact.get("declared_path", artifact.get("path")),
                        receipt=receipt,
                    ),
                    "sha256": artifact.get("sha256"),
                    "size_bytes": artifact.get("size_bytes"),
                }
            )
        experiments: list[dict[str, Any]] = []
        for raw_experiment in receipt.get("experiments", []):
            if not isinstance(raw_experiment, Mapping):
                continue
            experiment, experiment_errors = _experiment_proof_basis(
                raw_experiment,
                case_id=case_id,
                replay_executor=replay_executor,
                artifact_aliases=artifact_aliases,
            )
            experiments.append(experiment)
            errors.extend(experiment_errors)
        hypotheses = [
            _hypothesis_receipt_proof_basis(
                item,
                artifact_aliases=artifact_aliases,
            )
            for item in receipt.get("hypothesis_refs", [])
            if isinstance(item, Mapping)
        ]
        falsification_attempts = sorted(
            [
                {
                    "hypothesis_id": _text(hypothesis.get("hypothesis_id")),
                    "attempt_id": _text(attempt.get("attempt_id")),
                    "claim": _text(attempt.get("claim")),
                    "baseline_experiment_id": _text(attempt.get("baseline_experiment_id")),
                    "challenge_experiment_id": _text(attempt.get("challenge_experiment_id")),
                    "disproof_condition": attempt.get("disproof_condition"),
                    "outcome": _text(attempt.get("outcome")),
                }
                for hypothesis in (
                    dossier.get("root_cause_hypotheses", [])
                    if isinstance(dossier.get("root_cause_hypotheses"), list)
                    else []
                )
                if isinstance(hypothesis, Mapping)
                for attempt in (
                    hypothesis.get("falsification_attempts", [])
                    if isinstance(hypothesis.get("falsification_attempts"), list)
                    else []
                )
                if isinstance(attempt, Mapping)
            ],
            key=_canonical_hash,
        )
        inspected_files: list[dict[str, Any]] = []
        for raw_file in receipt.get("inspected_files", []):
            if not isinstance(raw_file, Mapping):
                continue
            inspected = dict(raw_file)
            inspected_files.append(
                {
                    field: inspected.get(field)
                    for field in (
                        "path",
                        "sha256",
                        "git_blob_sha",
                        "size_bytes",
                        "bytes_observed",
                        "whole_file_observed",
                        "observed_content_sha256",
                        "observed_start_line",
                        "observed_end_line",
                    )
                }
            )
        inspected_symbols = [
            {
                "symbol": _text(item.get("symbol")),
                "path": _text(item.get("path")),
            }
            for item in receipt.get("inspected_symbols", [])
            if isinstance(item, Mapping)
        ]
        projected.append(
            {
                "case_id": _text(dossier.get("case_id")),
                "problem_id": _text(dossier.get("problem_id")),
                "repo_revision": _text(dossier.get("repo_revision")),
                "evidence_assignment": _origin_assignment_proof_basis(
                    dossier.get("evidence_assignment")
                ),
                "verification": {
                    "verification_method": receipt.get("verification_method"),
                    "status": receipt.get("status"),
                    "repo_revision": _text(receipt.get("repo_revision")),
                    "requested_repo_ref": _text(receipt.get("requested_repo_ref")),
                    "resolved_repo_ref": _text(receipt.get("resolved_repo_ref")),
                    "workspace_head": _text(receipt.get("workspace_head")),
                    "planning_workspace_head": _text(receipt.get("planning_workspace_head")),
                    "planning_workspace_clean": receipt.get("planning_workspace_clean"),
                    "workspace_overlay": _workspace_overlay_proof_basis(
                        receipt.get("workspace_overlay")
                    ),
                    "replay_isolation": replay_isolation,
                    "origin_atom_ids": _sorted_strings(receipt.get("origin_atom_ids")),
                    "assignment_sha256": receipt.get("assignment_sha256"),
                    "artifacts": sorted(artifacts, key=_canonical_hash),
                    "experiments": sorted(experiments, key=_canonical_hash),
                    "inspected_files": sorted(inspected_files, key=_canonical_hash),
                    "inspected_symbols": sorted(inspected_symbols, key=_canonical_hash),
                    "hypothesis_refs": sorted(hypotheses, key=_canonical_hash),
                    "falsification_attempts": falsification_attempts,
                    "causal_links": _sorted_records(receipt.get("causal_links")),
                    "test_selections": _sorted_records(receipt.get("test_selections")),
                    "control_verifications": _sorted_records(receipt.get("control_verifications")),
                    "failure_paths": _sorted_records(receipt.get("failure_paths")),
                    "atom_bindings": _sorted_records(receipt.get("atom_bindings")),
                },
            }
        )
    return sorted(projected, key=_canonical_hash), list(dict.fromkeys(errors))


def evaluate_shadow_invariants(
    *,
    backlog: dict[str, Any],
    atoms: list[dict[str, Any]],
    stage1: dict[str, Any],
    stage2: dict[str, Any],
    stage3: dict[str, Any],
    stage4: dict[str, Any],
    stage5: dict[str, Any],
    stage6: dict[str, Any],
    case_registry: dict[str, Any],
    trusted_runs_roots: tuple[Path, ...] = (),
    owner_roots: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Evaluate depth invariants for one complete non-exporting cycle."""
    failures: list[str] = []
    failures.extend(
        verify_problem_mining_evidence_receipt(
            stage1=stage1,
            atoms=atoms,
            require_live=True,
        )
    )
    for atom in atoms:
        atom_id = _text(atom.get("atom_id")) or "(missing)"
        severity = (_text(atom.get("severity_hint")) or "").casefold()
        disposition = _text(atom.get("disposition"))
        if severity in _HIGH_SEVERITIES and not atom_is_idea_originated(atom):
            disposition_errors = atom_disposition_receipt_errors(
                atom,
                require_decided=True,
            )
            if disposition not in ATOM_DISPOSITIONS:
                disposition_errors.append("disposition_invalid")
            if disposition_errors:
                failures.append(
                    f"high_severity_atom_without_explicit_disposition:{atom_id}:"
                    + ",".join(dict.fromkeys(disposition_errors))
                )
        role = _text(atom.get("evidence_role"))
        if role in _DERIVED_EVIDENCE_ROLES and disposition == "novel_case":
            if not _text(atom.get("novel_case_rationale")):
                failures.append(f"derived_atom_novel_without_decision:{atom_id}")

    meaningful_observations = [atom for atom in atoms if _is_meaningful_automated_observation(atom)]
    if not meaningful_observations:
        failures.append("shadow_qualification_no_observed_automated_evidence")
    unresolved_work_errors = _unresolved_high_severity_work_errors(
        atoms=atoms,
        backlog=backlog,
        stage1=stage1,
        stage2=stage2,
        stage3=stage3,
        case_registry=case_registry,
    )
    failures.extend(unresolved_work_errors)

    research_by_identity: dict[str, tuple[bool, list[str]]] = {}
    ready_research_proofs: list[dict[str, Any]] = []
    for dossier in _items(stage3):
        ready, reasons = assess_research_readiness(dossier)
        retained_ready, retained_reasons = verify_persisted_research_evidence(dossier)
        if not retained_ready:
            ready = False
            reasons = [
                *reasons,
                "retained_research_evidence_invalid",
                *[f"retained:{reason}" for reason in retained_reasons],
            ]
        if ready:
            ready_research_proofs.append(dossier)
        for identity in (_text(dossier.get("case_id")), _text(dossier.get("problem_id"))):
            if identity is not None:
                research_by_identity[identity] = (ready, reasons)

    tickets_raw = backlog.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    for ticket in tickets:
        if _text(ticket.get("stage")) != "ready_for_ticket":
            continue
        ready, reasons = assess_ticket_readiness(ticket)
        if not ready:
            failures.append(
                "ready_ticket_failed_authoritative_readiness:"
                f"{_text(ticket.get('case_id')) or _text(ticket.get('problem_id'))}:"
                + ",".join(reasons)
            )
        identity = _text(ticket.get("case_id")) or _text(ticket.get("problem_id"))
        research = research_by_identity.get(identity or "")
        if research is None or not research[0]:
            failures.append(f"ready_ticket_without_ready_research:{identity}")

    plan_ids = {
        _text(plan.get("plan_revision_id"))
        for plan in _items(stage6)
        if _text(plan.get("plan_revision_id")) is not None
    }
    for ticket in tickets:
        if _text(ticket.get("stage")) != "ready_for_ticket":
            continue
        revision_id = _text(ticket.get("plan_revision_id"))
        if revision_id is None or revision_id not in plan_ids:
            failures.append(f"ready_ticket_missing_persisted_plan:{revision_id}")

    failures.extend(_relation_application_errors(stage1))
    failures.extend(
        _terminal_outcome_errors(
            case_registry,
            trusted_runs_roots=trusted_runs_roots,
            owner_roots=owner_roots,
        )
    )
    failures.extend(
        _case_conservation_errors(
            backlog=backlog,
            stage1=stage1,
            stage2=stage2,
            stage3=stage3,
            stage4=stage4,
            stage5=stage5,
            stage6=stage6,
            case_registry=case_registry,
        )
    )
    research_proof_basis, proof_basis_errors = _research_proof_basis_projection(
        ready_research_proofs
    )
    failures.extend(proof_basis_errors)
    failures = list(dict.fromkeys(failures))

    atom_projection = _atom_corpus_projection(atoms)
    source_atom_projection = _atom_corpus_projection(
        [atom for atom in atoms if _text(atom.get("evidence_role")) not in _DERIVED_EVIDENCE_ROLES]
    )
    source_atom_ids = {
        atom_id
        for atom in atoms
        if _text(atom.get("evidence_role")) not in _DERIVED_EVIDENCE_ROLES
        for atom_id in [_text(atom.get("atom_id"))]
        if atom_id is not None
    }
    case_graph = _case_graph_projection(
        case_registry,
        source_atom_ids=source_atom_ids,
    )
    return {
        "schema_version": 3,
        "passed": not failures,
        "failures": failures,
        "checks": {
            "high_severity_atom_dispositions": True,
            "problem_mining_full_evidence_partition": True,
            "derived_evidence_lineage": True,
            "usable_automated_work": True,
            "research_gate": True,
            "ready_plan_grounding": True,
            "relation_application": True,
            "terminal_outcome_provenance": True,
            "cross_stage_case_conservation": True,
            "research_proof_basis": True,
        },
        "atom_corpus_sha256": _canonical_hash(atom_projection),
        "source_atom_corpus_sha256": _canonical_hash(source_atom_projection),
        "case_graph_sha256": _canonical_hash(case_graph),
        "ticket_set_sha256": _canonical_hash(
            _ticket_projection(backlog, source_atom_ids=source_atom_ids)
        ),
        "research_proof_basis_sha256": _canonical_hash(research_proof_basis),
        "counts": {
            "atoms": len(atoms),
            "cases": len(case_graph),
            "research_proofs": len(_items(stage3)),
            "change_plans": len(_items(stage6)),
            "tickets": len(tickets),
            "ready_tickets": sum(
                1 for ticket in tickets if _text(ticket.get("stage")) == "ready_for_ticket"
            ),
            "qualifying_observed_atoms": len(meaningful_observations),
            "unresolved_high_severity_without_active_work": len(unresolved_work_errors),
        },
    }


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _invariant_projection(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": report.get("schema_version"),
        "passed": report.get("passed"),
        "failures": report.get("failures"),
        "checks": report.get("checks"),
        **{field: report.get(field) for field in _INVARIANT_HASH_FIELDS},
        "export_projection_sha256": report.get("export_projection_sha256"),
        "export_inputs_sha256": report.get("export_inputs_sha256"),
        "counts": report.get("counts"),
    }


def _invariant_report_errors(report: Any) -> list[str]:
    if not isinstance(report, dict):
        return ["shadow_invariant_report_invalid"]
    errors: list[str] = []
    if report.get("schema_version") != 3:
        errors.append("shadow_invariant_report_schema_invalid")
    failures = report.get("failures")
    if not isinstance(failures, list) or any(
        not isinstance(failure, str) or not failure.strip() for failure in failures
    ):
        errors.append("shadow_invariant_failures_invalid")
        failures = []
    passed = report.get("passed")
    if not isinstance(passed, bool):
        errors.append("shadow_invariant_passed_invalid")
    elif passed is (bool(failures)):
        errors.append("shadow_invariant_passed_failures_contradictory")
    checks = report.get("checks")
    if not isinstance(checks, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, bool)
        for key, value in checks.items()
    ):
        errors.append("shadow_invariant_checks_invalid")
    for field in _INVARIANT_HASH_FIELDS:
        if not _valid_sha256(report.get(field)):
            errors.append(f"shadow_invariant_hash_invalid:{field}")
    export_projection = report.get("export_projection_sha256")
    if not _valid_sha256(export_projection):
        errors.append("shadow_invariant_hash_invalid:export_projection_sha256")
    export_inputs = report.get("export_inputs_sha256")
    if export_inputs is not None and not _valid_sha256(export_inputs):
        errors.append("shadow_invariant_hash_invalid:export_inputs_sha256")
    counts = report.get("counts")
    if not isinstance(counts, dict) or any(
        not isinstance(key, str)
        or not key
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for key, value in counts.items()
    ):
        errors.append("shadow_invariant_counts_invalid")
    return errors


def _artifact_source_receipts(
    artifact_paths: Mapping[str, Path | None],
) -> list[dict[str, Any]]:
    names = set(artifact_paths)
    missing = sorted(_REQUIRED_SHADOW_ARTIFACTS - names)
    if missing:
        raise ValueError("shadow_cycle_required_artifacts_missing:" + ",".join(missing))
    receipts: list[dict[str, Any]] = []
    for name, raw_path in sorted(artifact_paths.items()):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("shadow_cycle_artifact_name_invalid")
        if raw_path is not None and not isinstance(raw_path, Path):
            raise ValueError(f"shadow_cycle_artifact_path_invalid:{name}")
        path = raw_path.resolve() if isinstance(raw_path, Path) else None
        exists = path is not None and path.is_file()
        if name in _REQUIRED_SHADOW_ARTIFACTS and not exists:
            raise ValueError(f"shadow_cycle_required_artifact_unavailable:{name}")
        if path is not None and path.exists() and not path.is_file():
            raise ValueError(f"shadow_cycle_artifact_not_file:{name}")
        receipts.append(
            {
                "name": name,
                "source_path": str(path) if path is not None else None,
                "exists": exists,
                "sha256": _file_sha256(path) if exists and path is not None else None,
                "content_sha256": (
                    _json_content_sha256(path) if exists and path is not None else None
                ),
                "size_bytes": path.stat().st_size if exists and path is not None else None,
            }
        )
    return receipts


def _cycle_identity_payload(cycle: dict[str, Any]) -> dict[str, Any]:
    receipts_raw = cycle.get("artifact_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    return {
        "cycle_schema_version": cycle.get("cycle_schema_version"),
        "run_identity_sha256": cycle.get("run_identity_sha256"),
        "generated_at": cycle.get("generated_at"),
        "invariant_report_sha256": cycle.get("invariant_report_sha256"),
        "backlog_path": cycle.get("backlog_path"),
        "backlog_sha256": cycle.get("backlog_sha256"),
        "backlog_content_sha256": cycle.get("backlog_content_sha256"),
        "required_consecutive_cycles": cycle.get("required_consecutive_cycles"),
        "require_exact_export_projection": cycle.get("require_exact_export_projection"),
        "artifact_receipts": [
            {
                "name": receipt.get("name"),
                "source_path": receipt.get("source_path"),
                "exists": receipt.get("exists"),
                "sha256": receipt.get("sha256"),
                "content_sha256": receipt.get("content_sha256"),
                "size_bytes": receipt.get("size_bytes"),
            }
            for receipt in receipts
            if isinstance(receipt, dict)
        ],
    }


def _artifact_source_projection(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: receipt.get(key)
            for key in (
                "name",
                "source_path",
                "exists",
                "sha256",
                "content_sha256",
                "size_bytes",
            )
        }
        for receipt in receipts
    ]


def _export_input_projection(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project only decision inputs; generated stage outputs have semantic gates."""
    return _artifact_source_projection(
        [receipt for receipt in receipts if receipt.get("name") not in _REQUIRED_SHADOW_ARTIFACTS]
    )


def _run_identity_sha256(cycle: dict[str, Any]) -> str:
    payload = _cycle_identity_payload(cycle)
    payload.pop("cycle_schema_version", None)
    payload.pop("run_identity_sha256", None)
    payload.pop("invariant_report_sha256", None)
    payload.pop("required_consecutive_cycles", None)
    payload.pop("require_exact_export_projection", None)
    return _canonical_hash(payload)


def _cycle_provenance_root(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.stem}.cycles")


def _materialize_cycle_provenance(
    *,
    state_path: Path,
    cycle: dict[str, Any],
    backlog_path: Path,
) -> dict[str, Any]:
    cycle_id = str(cycle["cycle_id"])
    cycle_dir = _cycle_provenance_root(state_path) / cycle_id
    try:
        cycle_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(f"shadow_cycle_duplicate:{cycle_id}") from exc
    try:
        backlog_snapshot = cycle_dir / "backlog.json"
        shutil.copyfile(backlog_path, backlog_snapshot)
        if _file_sha256(backlog_snapshot) != cycle["backlog_sha256"]:
            raise ValueError("shadow_cycle_backlog_changed_during_snapshot")
        cycle["backlog_snapshot_path"] = str(backlog_snapshot.resolve())
        receipts = cycle["artifact_receipts"]
        for index, receipt in enumerate(receipts):
            if receipt.get("exists") is not True:
                receipt["snapshot_path"] = None
                continue
            source_path = Path(str(receipt["source_path"]))
            suffix = source_path.suffix if source_path.suffix else ".artifact"
            name_hash = sha256(str(receipt["name"]).encode()).hexdigest()[:8]
            snapshot = cycle_dir / f"artifact_{index:03d}_{name_hash}{suffix}"
            shutil.copyfile(source_path, snapshot)
            if (
                _file_sha256(snapshot) != receipt["sha256"]
                or _json_content_sha256(snapshot) != receipt["content_sha256"]
                or snapshot.stat().st_size != receipt["size_bytes"]
            ):
                raise ValueError(f"shadow_cycle_artifact_changed_during_snapshot:{receipt['name']}")
            receipt["snapshot_path"] = str(snapshot.resolve())
        receipt_payload = {
            key: value
            for key, value in cycle.items()
            if key not in {"cycle_receipt_path", "cycle_receipt_sha256"}
        }
        receipt_path = cycle_dir / "cycle_receipt.json"
        receipt_path.write_text(
            json.dumps(receipt_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        cycle["cycle_receipt_path"] = str(receipt_path.resolve())
        cycle["cycle_receipt_sha256"] = _file_sha256(receipt_path)
    except Exception:
        shutil.rmtree(cycle_dir, ignore_errors=True)
        raise
    return cycle


def _artifact_receipt_errors(receipt: Any, *, verify_snapshot: bool) -> list[str]:
    if not isinstance(receipt, dict):
        return ["shadow_cycle_artifact_receipt_invalid"]
    name = receipt.get("name")
    exists = receipt.get("exists")
    errors: list[str] = []
    expected_fields = {
        "name",
        "source_path",
        "exists",
        "sha256",
        "content_sha256",
        "size_bytes",
        "snapshot_path",
    }
    if set(receipt) != expected_fields:
        errors.append(f"shadow_cycle_artifact_receipt_fields_invalid:{name}")
    if not isinstance(name, str) or not name.strip():
        errors.append("shadow_cycle_artifact_name_invalid")
    if not isinstance(exists, bool):
        errors.append(f"shadow_cycle_artifact_exists_invalid:{name}")
        return errors
    source_path = receipt.get("source_path")
    if source_path is not None and (not isinstance(source_path, str) or not source_path.strip()):
        errors.append(f"shadow_cycle_artifact_source_path_invalid:{name}")
    if exists:
        if not _valid_sha256(receipt.get("sha256")):
            errors.append(f"shadow_cycle_artifact_hash_invalid:{name}")
        content_hash = receipt.get("content_sha256")
        if content_hash is not None and not _valid_sha256(content_hash):
            errors.append(f"shadow_cycle_artifact_content_hash_invalid:{name}")
        size = receipt.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            errors.append(f"shadow_cycle_artifact_size_invalid:{name}")
        snapshot_raw = receipt.get("snapshot_path")
        snapshot = Path(snapshot_raw) if isinstance(snapshot_raw, str) else None
        if snapshot is None:
            errors.append(f"shadow_cycle_artifact_snapshot_missing:{name}")
        elif verify_snapshot and (
            not snapshot.is_file()
            or receipt.get("sha256") != _file_sha256(snapshot)
            or receipt.get("content_sha256") != _json_content_sha256(snapshot)
            or receipt.get("size_bytes") != snapshot.stat().st_size
        ):
            errors.append(f"shadow_cycle_artifact_snapshot_changed:{name}")
    elif any(
        receipt.get(field) is not None
        for field in ("sha256", "content_sha256", "size_bytes", "snapshot_path")
    ):
        errors.append(f"shadow_cycle_absent_artifact_has_receipt:{name}")
    return errors


def _cycle_row_errors(cycle: Any, *, verify_provenance: bool) -> list[str]:
    if not isinstance(cycle, dict):
        return ["shadow_cycle_row_invalid"]
    errors: list[str] = []
    missing_fields = sorted(_SHADOW_CYCLE_FIELDS - set(cycle))
    unknown_fields = sorted(set(cycle) - _SHADOW_CYCLE_FIELDS)
    if missing_fields:
        errors.append("shadow_cycle_fields_missing:" + ",".join(missing_fields))
    if unknown_fields:
        errors.append("shadow_cycle_fields_unknown:" + ",".join(unknown_fields))
    if cycle.get("cycle_schema_version") != _SHADOW_CYCLE_SCHEMA_VERSION:
        errors.append("shadow_cycle_schema_invalid")
    if not _valid_sha256(cycle.get("run_identity_sha256")) or cycle.get(
        "run_identity_sha256"
    ) != _run_identity_sha256(cycle):
        errors.append("shadow_cycle_run_identity_invalid")
    cycle_id = cycle.get("cycle_id")
    if not _valid_sha256(cycle_id):
        errors.append("shadow_cycle_id_invalid")
    elif cycle_id != _canonical_hash(_cycle_identity_payload(cycle)):
        errors.append(f"shadow_cycle_id_mismatch:{cycle_id}")
    invariant_projection = _invariant_projection(cycle)
    errors.extend(_invariant_report_errors(invariant_projection))
    if cycle.get("invariant_report_sha256") != _canonical_hash(invariant_projection):
        errors.append(f"shadow_cycle_invariant_hash_mismatch:{cycle_id}")
    if not _valid_sha256(cycle.get("backlog_sha256")):
        errors.append(f"shadow_cycle_backlog_hash_invalid:{cycle_id}")
    if not _valid_sha256(cycle.get("backlog_content_sha256")):
        errors.append(f"shadow_cycle_backlog_content_hash_invalid:{cycle_id}")
    if _text(cycle.get("generated_at")) is None or _text(cycle.get("backlog_path")) is None:
        errors.append(f"shadow_cycle_identity_fields_invalid:{cycle_id}")
    if (
        isinstance(cycle.get("required_consecutive_cycles"), bool)
        or not isinstance(cycle.get("required_consecutive_cycles"), int)
        or cycle.get("required_consecutive_cycles", 0) < 1
        or not isinstance(cycle.get("require_exact_export_projection"), bool)
    ):
        errors.append(f"shadow_cycle_settings_invalid:{cycle_id}")
    receipts_raw = cycle.get("artifact_receipts")
    if not isinstance(receipts_raw, list):
        errors.append(f"shadow_cycle_artifact_receipts_invalid:{cycle_id}")
        receipts: list[Any] = []
    else:
        receipts = receipts_raw
    names: list[str] = []
    for receipt in receipts:
        errors.extend(_artifact_receipt_errors(receipt, verify_snapshot=verify_provenance))
        if isinstance(receipt, dict) and isinstance(receipt.get("name"), str):
            names.append(receipt["name"])
    if len(names) != len(set(names)):
        errors.append(f"shadow_cycle_artifact_names_duplicate:{cycle_id}")
    if not _REQUIRED_SHADOW_ARTIFACTS.issubset(names):
        errors.append(f"shadow_cycle_required_artifacts_missing:{cycle_id}")
    valid_receipts = [receipt for receipt in receipts if isinstance(receipt, dict)]
    if cycle.get("export_inputs_sha256") != _canonical_hash(
        _export_input_projection(valid_receipts)
    ):
        errors.append(f"shadow_cycle_export_inputs_hash_mismatch:{cycle_id}")
    if verify_provenance:
        backlog_snapshot_raw = cycle.get("backlog_snapshot_path")
        backlog_snapshot = (
            Path(backlog_snapshot_raw) if isinstance(backlog_snapshot_raw, str) else None
        )
        if (
            backlog_snapshot is None
            or not backlog_snapshot.is_file()
            or cycle.get("backlog_sha256") != _file_sha256(backlog_snapshot)
            or cycle.get("backlog_content_sha256") != _backlog_content_sha256(backlog_snapshot)
        ):
            errors.append(f"shadow_cycle_backlog_snapshot_changed:{cycle_id}")
        receipt_path_raw = cycle.get("cycle_receipt_path")
        receipt_path = Path(receipt_path_raw) if isinstance(receipt_path_raw, str) else None
        if (
            receipt_path is None
            or not receipt_path.is_file()
            or cycle.get("cycle_receipt_sha256") != _file_sha256(receipt_path)
        ):
            errors.append(f"shadow_cycle_receipt_changed:{cycle_id}")
        else:
            try:
                receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                receipt_payload = None
            expected_payload = {
                key: value
                for key, value in cycle.items()
                if key not in {"cycle_receipt_path", "cycle_receipt_sha256"}
            }
            if receipt_payload != expected_payload:
                errors.append(f"shadow_cycle_receipt_payload_mismatch:{cycle_id}")
    return errors


def _cycle_matches_settings(
    cycle: dict[str, Any],
    *,
    required_consecutive_cycles: int,
    require_exact_export_projection: bool,
) -> bool:
    return (
        cycle.get("required_consecutive_cycles") == required_consecutive_cycles
        and cycle.get("require_exact_export_projection") is require_exact_export_projection
    )


def _same_stability_basis(
    cycle: dict[str, Any],
    reference: dict[str, Any],
    *,
    require_exact_export_projection: bool,
) -> bool:
    # ``require_exact_export_projection`` is retained as a compatible configuration
    # knob. Exactness now means exact canonical plan intent; the latest full rendered
    # projection is still bound at export but is not a cross-cycle prose hash.
    del require_exact_export_projection
    for field in (
        "source_atom_corpus_sha256",
        "case_graph_sha256",
        "ticket_set_sha256",
        "research_proof_basis_sha256",
    ):
        value = cycle.get(field)
        if not isinstance(value, str) or not value or value != reference.get(field):
            return False
    reference_export_inputs = reference.get("export_inputs_sha256")
    if (
        not _valid_sha256(reference_export_inputs)
        or cycle.get("export_inputs_sha256") != reference_export_inputs
    ):
        return False
    return True


def _consecutive_stable_passes(
    cycles: list[dict[str, Any]],
    *,
    required_consecutive_cycles: int,
    require_exact_export_projection: bool,
) -> int:
    if not cycles:
        return 0
    reference = cycles[-1]
    if reference.get("passed") is not True or not _cycle_matches_settings(
        reference,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    ):
        return 0

    count = 0
    for cycle in reversed(cycles):
        if cycle.get("passed") is not True:
            break
        if not _cycle_matches_settings(
            cycle,
            required_consecutive_cycles=required_consecutive_cycles,
            require_exact_export_projection=require_exact_export_projection,
        ):
            break
        if not _same_stability_basis(
            cycle,
            reference,
            require_exact_export_projection=require_exact_export_projection,
        ):
            break
        count += 1
    return count


def _state_document_errors(state: Any, *, verify_provenance: bool) -> list[str]:
    if not isinstance(state, dict):
        return ["shadow_state_invalid"]
    errors: list[str] = []
    if set(state) != _SHADOW_STATE_FIELDS:
        errors.append("shadow_state_fields_invalid")
    if state.get("schema_version") != _SHADOW_STATE_SCHEMA_VERSION:
        errors.append("shadow_state_schema_unsupported")
    if _text(state.get("backlog_path")) is None:
        errors.append("shadow_state_backlog_path_invalid")
    if not isinstance(state.get("ready_for_export"), bool):
        errors.append("shadow_state_ready_flag_invalid")
    required_cycles = state.get("required_consecutive_cycles")
    exact = state.get("require_exact_export_projection")
    if (
        isinstance(required_cycles, bool)
        or not isinstance(required_cycles, int)
        or required_cycles < 1
        or not isinstance(exact, bool)
    ):
        errors.append("shadow_state_settings_invalid")
        required_cycles = _DEFAULT_REQUIRED_CONSECUTIVE_CYCLES
        exact = _DEFAULT_REQUIRE_EXACT_EXPORT_PROJECTION
    cycles_raw = state.get("cycles")
    if not isinstance(cycles_raw, list) or any(not isinstance(cycle, dict) for cycle in cycles_raw):
        errors.append("shadow_state_cycles_invalid")
        cycles: list[dict[str, Any]] = []
    else:
        cycles = cycles_raw
    cycle_ids: list[str] = []
    for cycle in cycles:
        errors.extend(_cycle_row_errors(cycle, verify_provenance=verify_provenance))
        if isinstance(cycle.get("cycle_id"), str):
            cycle_ids.append(cycle["cycle_id"])
    if len(cycle_ids) != len(set(cycle_ids)):
        errors.append("shadow_state_duplicate_cycle_ids")
    consecutive = _consecutive_stable_passes(
        cycles,
        required_consecutive_cycles=required_cycles,
        require_exact_export_projection=exact,
    )
    recorded_consecutive = state.get("consecutive_stable_passes")
    if isinstance(recorded_consecutive, bool) or not isinstance(recorded_consecutive, int):
        errors.append("shadow_state_consecutive_count_invalid")
    computed_ready = consecutive >= required_cycles
    if recorded_consecutive != consecutive:
        errors.append("shadow_state_consecutive_count_mismatch")
    if state.get("ready_for_export") is not computed_ready:
        errors.append("shadow_state_readiness_mismatch")
    validated_fields = (
        "validated_cycle_id",
        "validated_backlog_sha256",
        "validated_backlog_content_sha256",
        "validated_export_inputs_sha256",
        "validated_research_proof_basis_sha256",
    )
    if computed_ready and cycles:
        latest = cycles[-1]
        expected = {
            "validated_cycle_id": latest.get("cycle_id"),
            "validated_backlog_sha256": latest.get("backlog_sha256"),
            "validated_backlog_content_sha256": latest.get("backlog_content_sha256"),
            "validated_export_inputs_sha256": latest.get("export_inputs_sha256"),
            "validated_research_proof_basis_sha256": latest.get("research_proof_basis_sha256"),
        }
        for field, value in expected.items():
            if state.get(field) != value:
                errors.append(f"shadow_state_{field}_mismatch")
    elif any(state.get(field) is not None for field in validated_fields):
        errors.append("shadow_state_unqualified_validated_hashes")
    return errors


def _current_artifact_binding_errors(
    cycle: dict[str, Any],
    *,
    artifact_paths: Mapping[str, Path | None] | None,
) -> list[str]:
    errors: list[str] = []
    receipts_raw = cycle.get("artifact_receipts")
    receipts = receipts_raw if isinstance(receipts_raw, list) else []
    receipts_by_name = {
        str(receipt.get("name")): receipt
        for receipt in receipts
        if isinstance(receipt, dict) and isinstance(receipt.get("name"), str)
    }
    for name, receipt in receipts_by_name.items():
        source_raw = receipt.get("source_path")
        source = Path(source_raw) if isinstance(source_raw, str) else None
        if receipt.get("exists") is True:
            if (
                source is None
                or not source.is_file()
                or receipt.get("sha256") != _file_sha256(source)
                or receipt.get("content_sha256") != _json_content_sha256(source)
                or receipt.get("size_bytes") != source.stat().st_size
            ):
                errors.append(f"shadow_cycle_source_artifact_changed:{name}")
        elif source is not None and source.exists():
            errors.append(f"shadow_cycle_absent_artifact_now_exists:{name}")
    if artifact_paths is not None:
        try:
            current_receipts = _artifact_source_receipts(artifact_paths)
        except (OSError, ValueError) as exc:
            errors.append(f"shadow_cycle_expected_artifacts_invalid:{exc}")
        else:
            expected_projection = _artifact_source_projection(receipts)
            if current_receipts != expected_projection:
                errors.append("shadow_cycle_expected_artifacts_mismatch")
    return errors


def record_shadow_cycle(
    *,
    state_path: Path,
    backlog_path: Path,
    invariant_report: dict[str, Any],
    artifact_paths: Mapping[str, Path | None],
    generated_at: str,
    required_consecutive_cycles: int = _DEFAULT_REQUIRED_CONSECUTIVE_CYCLES,
    require_exact_export_projection: bool = (_DEFAULT_REQUIRE_EXACT_EXPORT_PROJECTION),
) -> dict[str, Any]:
    """Append one artifact-bound runner cycle and compute activation readiness.

    The retained receipts detect integrity loss and accidental/local tampering. They
    are not cryptographic protection against an actor who can rewrite both state and
    every retained provenance artifact.
    """
    settings = normalize_shadow_gate_config(
        {
            "enabled": True,
            "required_consecutive_shadow_cycles": required_consecutive_cycles,
            "require_exact_export_projection": require_exact_export_projection,
        }
    )
    required_consecutive_cycles = settings["required_consecutive_shadow_cycles"]
    require_exact_export_projection = settings["require_exact_export_projection"]
    previous: dict[str, Any] = {}
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"shadow_state_unreadable:{type(exc).__name__}") from exc
        previous_errors = _state_document_errors(raw, verify_provenance=True)
        if previous_errors:
            raise ValueError("shadow_state_invalid:" + ",".join(previous_errors))
        previous = raw
    cycles_raw = previous.get("cycles", [])
    cycles = list(cycles_raw) if isinstance(cycles_raw, list) else []
    invariant_errors = _invariant_report_errors(invariant_report)
    if invariant_errors:
        raise ValueError("shadow_invariant_report_invalid:" + ",".join(invariant_errors))
    if _text(generated_at) is None:
        raise ValueError("shadow_cycle_generated_at_invalid")
    artifact_receipts = _artifact_source_receipts(artifact_paths)
    export_inputs_sha256 = _canonical_hash(_export_input_projection(artifact_receipts))
    invariant_projection = _invariant_projection(
        {
            **invariant_report,
            "export_inputs_sha256": export_inputs_sha256,
        }
    )
    cycle = {
        **invariant_projection,
        "cycle_schema_version": _SHADOW_CYCLE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "backlog_path": str(backlog_path.resolve()),
        "backlog_sha256": _file_sha256(backlog_path),
        "backlog_content_sha256": _backlog_content_sha256(backlog_path),
        "invariant_report_sha256": _canonical_hash(invariant_projection),
        "artifact_receipts": artifact_receipts,
        "required_consecutive_cycles": required_consecutive_cycles,
        "require_exact_export_projection": require_exact_export_projection,
    }
    cycle["run_identity_sha256"] = _run_identity_sha256(cycle)
    cycle["cycle_id"] = _canonical_hash(_cycle_identity_payload(cycle))
    if cycle["cycle_id"] in {
        previous_cycle.get("cycle_id")
        for previous_cycle in cycles
        if isinstance(previous_cycle, dict)
    }:
        raise ValueError(f"shadow_cycle_duplicate:{cycle['cycle_id']}")
    cycle = _materialize_cycle_provenance(
        state_path=state_path,
        cycle=cycle,
        backlog_path=backlog_path,
    )
    cycle_errors = _cycle_row_errors(cycle, verify_provenance=True)
    cycle_errors.extend(_current_artifact_binding_errors(cycle, artifact_paths=artifact_paths))
    if cycle.get("backlog_sha256") != _file_sha256(backlog_path) or cycle.get(
        "backlog_content_sha256"
    ) != _backlog_content_sha256(backlog_path):
        cycle_errors.append("shadow_cycle_backlog_changed_after_snapshot")
    if cycle_errors:
        receipt_path_raw = cycle.get("cycle_receipt_path")
        if isinstance(receipt_path_raw, str):
            shutil.rmtree(Path(receipt_path_raw).parent, ignore_errors=True)
        raise ValueError("shadow_cycle_invalid:" + ",".join(cycle_errors))
    cycles.append(cycle)
    cycles = cycles[-max(10, required_consecutive_cycles) :]
    consecutive_stable_passes = _consecutive_stable_passes(
        cycles,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    stable = consecutive_stable_passes >= required_consecutive_cycles
    state = {
        "schema_version": _SHADOW_STATE_SCHEMA_VERSION,
        "backlog_path": str(backlog_path.resolve()),
        "ready_for_export": stable,
        "required_consecutive_cycles": required_consecutive_cycles,
        "require_exact_export_projection": require_exact_export_projection,
        "consecutive_stable_passes": consecutive_stable_passes,
        "validated_cycle_id": cycle["cycle_id"] if stable else None,
        "validated_backlog_sha256": cycle["backlog_sha256"] if stable else None,
        "validated_backlog_content_sha256": (cycle["backlog_content_sha256"] if stable else None),
        "validated_export_inputs_sha256": (cycle["export_inputs_sha256"] if stable else None),
        "validated_research_proof_basis_sha256": (
            cycle["research_proof_basis_sha256"] if stable else None
        ),
        "cycles": cycles,
    }
    state_errors = _state_document_errors(state, verify_provenance=True)
    if state_errors:
        raise ValueError("shadow_state_invalid:" + ",".join(state_errors))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return state


def validate_shadow_export_state(
    *,
    state_path: Path,
    backlog_path: Path,
    backlog_snapshot_sha256: str | None = None,
    backlog_snapshot_content_sha256: str | None = None,
    artifact_paths: Mapping[str, Path | None] | None = None,
    required_consecutive_cycles: int = _DEFAULT_REQUIRED_CONSECUTIVE_CYCLES,
    require_exact_export_projection: bool = (_DEFAULT_REQUIRE_EXACT_EXPORT_PROJECTION),
) -> tuple[bool, list[str], dict[str, Any] | None]:
    """Require the configured stable-cycle streak before exporting a backlog."""
    settings = normalize_shadow_gate_config(
        {
            "enabled": True,
            "required_consecutive_shadow_cycles": required_consecutive_cycles,
            "require_exact_export_projection": require_exact_export_projection,
        }
    )
    required_consecutive_cycles = settings["required_consecutive_shadow_cycles"]
    require_exact_export_projection = settings["require_exact_export_projection"]
    if not state_path.exists():
        return False, ["shadow_state_missing"], None
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"shadow_state_unreadable:{type(exc).__name__}"], None
    if not isinstance(raw, dict):
        return False, ["shadow_state_invalid"], None
    reasons = _state_document_errors(raw, verify_provenance=True)
    if (
        raw.get("required_consecutive_cycles") != required_consecutive_cycles
        or raw.get("require_exact_export_projection") is not require_exact_export_projection
    ):
        reasons.append("shadow_gate_config_changed")

    cycles_raw = raw.get("cycles")
    cycles = (
        cycles_raw
        if isinstance(cycles_raw, list) and all(isinstance(cycle, dict) for cycle in cycles_raw)
        else []
    )
    consecutive_stable_passes = _consecutive_stable_passes(
        cycles,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    computed_ready = consecutive_stable_passes >= required_consecutive_cycles
    if not computed_ready:
        reasons.append(
            "stable_shadow_cycles_required:"
            f"{consecutive_stable_passes}/{required_consecutive_cycles}"
        )
    if computed_ready:
        latest = cycles[-1] if cycles and isinstance(cycles[-1], dict) else {}
        reasons.extend(
            _current_artifact_binding_errors(
                latest,
                artifact_paths=artifact_paths,
            )
        )
        try:
            current_hash = backlog_snapshot_sha256 or _file_sha256(backlog_path)
            current_content_hash = backlog_snapshot_content_sha256 or _backlog_content_sha256(
                backlog_path
            )
        except (OSError, ValueError, json.JSONDecodeError):
            reasons.append("backlog_hash_unavailable")
        else:
            if not _valid_sha256(current_hash) or not _valid_sha256(current_content_hash):
                reasons.append("backlog_snapshot_hash_invalid")
            if (
                raw.get("validated_backlog_sha256") != latest.get("backlog_sha256")
                or raw.get("validated_backlog_content_sha256")
                != latest.get("backlog_content_sha256")
                or raw.get("validated_cycle_id") != latest.get("cycle_id")
            ):
                reasons.append("shadow_state_validated_cycle_mismatch")
            if (
                latest.get("backlog_sha256") != current_hash
                or latest.get("backlog_content_sha256") != current_content_hash
            ):
                reasons.append("backlog_changed_since_shadow_validation")
    if _text(raw.get("backlog_path")) != str(backlog_path.resolve()):
        reasons.append("shadow_state_backlog_path_mismatch")
    return not reasons, list(dict.fromkeys(reasons)), raw


__all__ = [
    "evaluate_shadow_invariants",
    "normalize_shadow_gate_config",
    "record_shadow_cycle",
    "shadow_state_path",
    "validate_shadow_export_state",
]
