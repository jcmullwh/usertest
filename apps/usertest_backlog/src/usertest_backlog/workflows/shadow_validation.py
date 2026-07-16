"""Shadow-cycle invariants and export activation gate.

Shadow runs execute the complete backlog analysis without exporting implementation
tickets. A configurable number of consecutive runs must pass the depth invariants and
preserve the same source-observation corpus and independently adjudicated
recovered/missed outcome for each sealed source-actionable group. Full case graphs,
backlog, ticket-intent, and research-proof hashes remain bound to each cycle and the
latest export, but different independently-good nondeterministic groupings and
mechanisms do not reset the semantic streak.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core import (
    assess_research_readiness,
    assess_ticket_readiness,
    plan_revision_id_for,
    research_claims_sha256,
)
from backlog_core.case_lineage import (
    ATOM_DISPOSITIONS,
    atom_disposition_receipt_errors,
    atom_is_idea_originated,
    atom_is_independent_problem_evidence,
)
from backlog_miner.pipeline import verify_stage_model_invocation_contract
from backlog_miner.research_evidence import verify_persisted_research_evidence
from backlog_repo import validate_case_relation_receipt, verify_outcome_record_provenance
from backlog_repo.plan_scope import validate_plan_target_contract

from usertest_backlog.workflows.post_research_relations import (
    verified_causal_evidence_projection,
)
from usertest_backlog.workflows.problem_mining_evidence import (
    immutable_atom_evidence_projection,
    verify_problem_mining_evidence_receipt,
)
from usertest_backlog.workflows.qualification import (
    evaluate_independent_qualification,
    qualification_output_causal_target,
)
from usertest_backlog.workflows.qualification_healing import (
    qualification_correction_route_errors,
)

_DERIVED_EVIDENCE_ROLES = frozenset({"research", "implementation", "verification"})
_HIGH_SEVERITIES = frozenset({"high", "blocker"})
_CASE_TERMINAL_OUTCOMES = frozenset({"resolved", "duplicate", "superseded"})
_SHADOW_STATE_SCHEMA_VERSION = 11
_SHADOW_CYCLE_SCHEMA_VERSION = 9
_DEFAULT_REQUIRED_CONSECUTIVE_CYCLES = 2
_DEFAULT_REQUIRE_EXACT_EXPORT_PROJECTION = True
_DEFAULT_REQUIRE_NONEMPTY_THROUGHPUT = True
_DEFAULT_MINIMUM_EVIDENCE_SUFFICIENT_RESEARCH_PROOFS = 1
_DEFAULT_MINIMUM_AUTHORITATIVE_READY_TICKETS = 1
_DEFAULT_MINIMUM_GOOD_TICKET_COUNT = 1
_DEFAULT_MINIMUM_GOOD_TO_BAD_RATIO = 2.0
_DEFAULT_MINIMUM_RECOVERED_TO_MISSED_RATIO = 2.0
_DEFAULT_REQUIRE_ZERO_UNKNOWN_AUTHORITATIVE_TICKETS = True
_DEFAULT_FAIL_ON_SYSTEMIC_RESEARCH_BLOCKERS = True
_PENDING_SHADOW_RUN_SCHEMA_VERSION = 1
_PENDING_OPERATIONAL_SHADOW_RUN_SCHEMA_VERSION = 1
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
    "qualification_basis_sha256",
    "qualification_stability_sha256",
)
_SHADOW_CYCLE_FIELDS = frozenset(
    {
        "cycle_schema_version",
        "cycle_mode",
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
        "stability_inputs_sha256",
        "qualification",
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
        "activation_mode",
        "release_anchor_cycle_ids",
        "release_anchor_stability_inputs_sha256",
        "validated_cycle_id",
        "validated_backlog_sha256",
        "validated_backlog_content_sha256",
        "validated_export_inputs_sha256",
        "validated_research_proof_basis_sha256",
        "validated_qualification_basis_sha256",
        "cycles",
    }
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _clean_string_set(value: Any) -> set[str]:
    return {
        item.strip()
        for item in (value if isinstance(value, list) else [])
        if isinstance(item, str) and item.strip()
    }


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


def shadow_pending_run_path(backlog_path: Path) -> Path:
    """Return the immutable-input receipt used by two-phase shadow scoring."""

    stem = backlog_path.stem.removesuffix(".backlog")
    return backlog_path.with_name(f"{stem}.shadow_pending.json")


def operational_shadow_pending_run_path(backlog_path: Path) -> Path:
    """Return the receipt binding one fresh operational materialization."""

    stem = backlog_path.stem.removesuffix(".backlog")
    return backlog_path.with_name(f"{stem}.operational_shadow_pending.json")


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
        "require_nonempty_throughput",
        "minimum_evidence_sufficient_research_proofs",
        "minimum_authoritative_ready_tickets",
        "minimum_good_ticket_count",
        "minimum_good_to_bad_ratio",
        "minimum_recovered_to_missed_ratio",
        "require_zero_unknown_authoritative_tickets",
        "fail_on_systemic_research_blockers",
        "qualification_corpus_manifest_path",
        "qualification_output_adjudication_path",
        "no_actionable_evidence_receipt_path",
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

    throughput_raw = raw.get(
        "require_nonempty_throughput",
        _DEFAULT_REQUIRE_NONEMPTY_THROUGHPUT,
    )
    if not isinstance(throughput_raw, bool):
        raise ValueError("backlog_export_gate.require_nonempty_throughput must be a boolean")

    minimum_research_raw = raw.get(
        "minimum_evidence_sufficient_research_proofs",
        _DEFAULT_MINIMUM_EVIDENCE_SUFFICIENT_RESEARCH_PROOFS,
    )
    if (
        isinstance(minimum_research_raw, bool)
        or not isinstance(minimum_research_raw, int)
        or minimum_research_raw < 1
    ):
        raise ValueError(
            "backlog_export_gate.minimum_evidence_sufficient_research_proofs "
            "must be a positive integer"
        )

    minimum_tickets_raw = raw.get(
        "minimum_authoritative_ready_tickets",
        _DEFAULT_MINIMUM_AUTHORITATIVE_READY_TICKETS,
    )
    if (
        isinstance(minimum_tickets_raw, bool)
        or not isinstance(minimum_tickets_raw, int)
        or minimum_tickets_raw < 1
    ):
        raise ValueError(
            "backlog_export_gate.minimum_authoritative_ready_tickets must be a positive integer"
        )

    minimum_good_ticket_count_raw = raw.get(
        "minimum_good_ticket_count",
        _DEFAULT_MINIMUM_GOOD_TICKET_COUNT,
    )
    if (
        isinstance(minimum_good_ticket_count_raw, bool)
        or not isinstance(minimum_good_ticket_count_raw, int)
        or minimum_good_ticket_count_raw < 1
    ):
        raise ValueError("backlog_export_gate.minimum_good_ticket_count must be a positive integer")

    minimum_ratio_raw = raw.get(
        "minimum_good_to_bad_ratio",
        _DEFAULT_MINIMUM_GOOD_TO_BAD_RATIO,
    )
    if (
        isinstance(minimum_ratio_raw, bool)
        or not isinstance(minimum_ratio_raw, (int, float))
        or float(minimum_ratio_raw) <= 1.0
    ):
        raise ValueError(
            "backlog_export_gate.minimum_good_to_bad_ratio must be a number greater than 1"
        )

    recovered_ratio_raw = raw.get(
        "minimum_recovered_to_missed_ratio",
        _DEFAULT_MINIMUM_RECOVERED_TO_MISSED_RATIO,
    )
    if (
        isinstance(recovered_ratio_raw, bool)
        or not isinstance(recovered_ratio_raw, (int, float))
        or float(recovered_ratio_raw) <= 1.0
    ):
        raise ValueError(
            "backlog_export_gate.minimum_recovered_to_missed_ratio must be a number greater than 1"
        )

    zero_unknown_raw = raw.get(
        "require_zero_unknown_authoritative_tickets",
        _DEFAULT_REQUIRE_ZERO_UNKNOWN_AUTHORITATIVE_TICKETS,
    )
    if not isinstance(zero_unknown_raw, bool):
        raise ValueError(
            "backlog_export_gate.require_zero_unknown_authoritative_tickets must be a boolean"
        )

    systemic_blockers_raw = raw.get(
        "fail_on_systemic_research_blockers",
        _DEFAULT_FAIL_ON_SYSTEMIC_RESEARCH_BLOCKERS,
    )
    if not isinstance(systemic_blockers_raw, bool):
        raise ValueError("backlog_export_gate.fail_on_systemic_research_blockers must be a boolean")

    artifact_paths: dict[str, str | None] = {}
    for field in (
        "qualification_corpus_manifest_path",
        "qualification_output_adjudication_path",
        "no_actionable_evidence_receipt_path",
    ):
        value = raw.get(field)
        if value is not None and _text(value) is None:
            raise ValueError(f"backlog_export_gate.{field} must be a non-empty string or null")
        artifact_paths[field] = _text(value)

    return {
        "enabled": enabled_raw,
        "required_consecutive_shadow_cycles": required_raw,
        "require_exact_export_projection": exact_raw,
        "require_nonempty_throughput": throughput_raw,
        "minimum_evidence_sufficient_research_proofs": minimum_research_raw,
        "minimum_authoritative_ready_tickets": minimum_tickets_raw,
        "minimum_good_ticket_count": minimum_good_ticket_count_raw,
        "minimum_good_to_bad_ratio": float(minimum_ratio_raw),
        "minimum_recovered_to_missed_ratio": float(recovered_ratio_raw),
        "require_zero_unknown_authoritative_tickets": zero_unknown_raw,
        "fail_on_systemic_research_blockers": systemic_blockers_raw,
        **artifact_paths,
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


def _shadow_qualification_contract(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only the throughput settings consumed by invariant evaluation."""

    settings = normalize_shadow_gate_config(dict(raw) if raw is not None else None)
    return {
        "require_nonempty_throughput": settings["require_nonempty_throughput"],
        "minimum_evidence_sufficient_research_proofs": settings[
            "minimum_evidence_sufficient_research_proofs"
        ],
        "minimum_authoritative_ready_tickets": settings["minimum_authoritative_ready_tickets"],
        "minimum_good_ticket_count": settings["minimum_good_ticket_count"],
        "minimum_good_to_bad_ratio": settings["minimum_good_to_bad_ratio"],
        "minimum_recovered_to_missed_ratio": settings["minimum_recovered_to_missed_ratio"],
        "require_zero_unknown_authoritative_tickets": settings[
            "require_zero_unknown_authoritative_tickets"
        ],
        "fail_on_systemic_research_blockers": settings["fail_on_systemic_research_blockers"],
    }


def _role_run_author_provenance(
    run: Mapping[str, Any],
    *,
    authoring_stage: str,
    case_id: str | None,
    problem_id: str | None,
) -> dict[str, Any] | None:
    session_id = _text(run.get("session_id"))
    attempts_raw = run.get("attempt_history")
    attempts = (
        [item for item in attempts_raw if isinstance(item, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    retained = next(
        (attempt for attempt in reversed(attempts) if attempt.get("status") == "verified"),
        attempts[-1] if attempts else None,
    )
    observed_sessions = {
        value
        for attempt in attempts
        for value in [_text(attempt.get("agent_session_id"))]
        if value is not None
    }
    workspace_dir = (
        _text(retained.get("workspace_dir")) if isinstance(retained, Mapping) else None
    ) or _text(run.get("workspace_dir"))
    repository_revision = (
        _text(retained.get("repo_revision")) if isinstance(retained, Mapping) else None
    ) or _text(run.get("repo_revision"))
    continuity_verified = bool(
        session_id is not None
        and run.get("accepted") is True
        and (not observed_sessions or observed_sessions == {session_id})
        and (retained is None or retained.get("status") == "verified")
    )
    workspace_continuity_verified = bool(
        workspace_dir is not None
        and repository_revision is not None
        and all(
            _text(attempt.get("workspace_dir")) in {None, workspace_dir}
            and _text(attempt.get("repo_revision")) in {None, repository_revision}
            for attempt in attempts
        )
    )
    if session_id is None and retained is None:
        return None
    return {
        "provenance_source": "runner_stage_role_history",
        "authoring_stage": authoring_stage,
        "author_role": _text(run.get("role")),
        "case_id": case_id,
        "problem_id": problem_id,
        "agent_session_id": session_id,
        "exact_session_continuation": continuity_verified,
        "workspace_dir": workspace_dir,
        "repository_revision": repository_revision,
        "workspace_continuity_verified": workspace_continuity_verified,
        "original_author_cost_seconds": (
            max(0.0, float(attempts[0].get("elapsed_seconds") or 0.0))
            if attempts
            else 0.0
        ),
        "author_attempt_identity": (
            {
                "attempt_number": retained.get("attempt_number"),
                "attempt_tag": _text(retained.get("attempt_tag")),
                "prompt_sha256": retained.get("prompt_sha256"),
                "response_sha256": retained.get("response_sha256") or run.get("response_sha256"),
            }
            if isinstance(retained, Mapping)
            else {"response_sha256": run.get("response_sha256")}
        ),
    }


def _research_author_provenance(record: Mapping[str, Any]) -> dict[str, Any] | None:
    attempts_raw = record.get("research_attempts")
    attempts = (
        [item for item in attempts_raw if isinstance(item, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    if not attempts:
        return None
    final_attempt = attempts[-1]
    session_id = _text(final_attempt.get("agent_session_id"))
    observed_session = _text(final_attempt.get("observed_agent_session_id"))
    resumed_session = _text(final_attempt.get("resumed_from_session_id"))
    outcome = _text(final_attempt.get("outcome"))
    workspace_dir = _text(record.get("repo_workspace"))
    repository_revision = _text(record.get("repo_revision"))
    exact = bool(
        session_id is not None
        and outcome in {"output_contract_valid", "repair_contract_valid"}
        and observed_session in {None, session_id}
        and resumed_session in {None, session_id}
    )
    return {
        "provenance_source": "runner_research_attempt_history",
        "authoring_stage": "repro_research",
        "author_role": "researcher",
        "case_id": _text(record.get("case_id")),
        "problem_id": _text(record.get("problem_id")),
        "agent_session_id": session_id,
        "exact_session_continuation": exact,
        "workspace_dir": workspace_dir,
        "repository_revision": repository_revision,
        "workspace_continuity_verified": bool(
            workspace_dir is not None and repository_revision is not None
        ),
        "original_author_cost_seconds": max(
            0.0, float(final_attempt.get("attempt_wall_seconds") or 0.0)
        ),
        "author_attempt_identity": {
            "attempt_number": final_attempt.get("attempt_number"),
            "attempt_kind": _text(final_attempt.get("attempt_kind")),
            "attempt_sha256": final_attempt.get("attempt_sha256"),
            "attempted_dossier_sha256": final_attempt.get("attempted_dossier_sha256"),
        },
    }


def _retained_repair_author_meta(
    document: Mapping[str, Any],
    *,
    problem_ids: set[str],
) -> tuple[Mapping[str, Any], dict[str, Any] | None]:
    """Select the latest per-problem repaired author frontier before top-level meta."""

    top_meta_raw = document.get("input_meta")
    top_meta = top_meta_raw if isinstance(top_meta_raw, Mapping) else {}
    history_raw = top_meta.get("qualification_repair_history")
    history = (
        [item for item in history_raw if isinstance(item, Mapping)]
        if isinstance(history_raw, list)
        else []
    )
    for entry in reversed(history):
        affected = {
            value
            for value in (
                entry.get("affected_problem_ids")
                if isinstance(entry.get("affected_problem_ids"), list)
                else []
            )
            if isinstance(value, str) and value.strip()
        }
        replacement_meta = entry.get("replacement_author_input_meta")
        if affected.intersection(problem_ids) and isinstance(replacement_meta, Mapping):
            return replacement_meta, {
                "source": "qualification_repair_history",
                "affected_problem_ids": sorted(affected),
                "replacement_stage_document_sha256": entry.get(
                    "replacement_stage_document_sha256"
                ),
                "route_consumption_receipts": [
                    dict(item)
                    for item in entry.get("route_consumption_receipts", [])
                    if isinstance(item, Mapping)
                ],
            }
    return top_meta, None


def _attach_repair_frontier(
    provenance: dict[str, Any] | None,
    frontier: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if provenance is not None and isinstance(frontier, Mapping):
        provenance["qualification_repair_frontier"] = dict(frontier)
    return provenance


def _qualification_output_author_provenance(
    *,
    stage1: Mapping[str, Any],
    stage2: Mapping[str, Any],
    stage3: Mapping[str, Any],
    stage4: Mapping[str, Any],
    stage5: Mapping[str, Any],
    stage6: Mapping[str, Any],
    accepted_outputs_by_kind: Mapping[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any] | list[dict[str, Any]]]:
    """Bind accepted output hashes to runner-owned exact-author histories."""

    index: dict[str, dict[str, Any]] = {}

    def bind(kind: str, output: Mapping[str, Any], provenance: dict[str, Any] | None) -> None:
        if provenance is not None:
            bound = dict(provenance)
            bound["causal_target"] = qualification_output_causal_target(kind, output)
            index[f"{kind}:{_canonical_hash(output)}"] = bound

    stage1_meta_raw = stage1.get("input_meta")
    stage1_meta = stage1_meta_raw if isinstance(stage1_meta_raw, Mapping) else {}
    miners_raw = stage1_meta.get("miner_results")
    miners = (
        [item for item in miners_raw if isinstance(item, Mapping)]
        if isinstance(miners_raw, list)
        else []
    )
    relation_batches_raw = stage1_meta.get("relation_review_batches")
    relation_batches = (
        [item for item in relation_batches_raw if isinstance(item, Mapping)]
        if isinstance(relation_batches_raw, list)
        else []
    )
    for problem in accepted_outputs_by_kind.get("problem", []):
        evidence_ids = {
            str(item).strip()
            for item in (
                problem.get("evidence_atom_ids")
                if isinstance(problem.get("evidence_atom_ids"), list)
                else []
            )
            if isinstance(item, str) and item.strip()
        }
        problem_identity_ids = {
            value
            for value in [
                _text(problem.get("problem_id")),
                *[
                    _text(item)
                    for item in (
                        problem.get("case_member_problem_ids")
                        if isinstance(problem.get("case_member_problem_ids"), list)
                        else []
                    )
                ],
            ]
            if value is not None
        }
        problem_meta, repair_frontier = _retained_repair_author_meta(
            stage1,
            problem_ids=problem_identity_ids,
        )
        problem_miners_raw = problem_meta.get("miner_results")
        problem_miners = (
            [item for item in problem_miners_raw if isinstance(item, Mapping)]
            if isinstance(problem_miners_raw, list)
            else miners
        )
        frontiers: list[dict[str, Any]] = []
        for miner in problem_miners:
            assigned_ids = {
                str(value).strip()
                for value in (
                    miner.get("assigned_atom_ids")
                    if isinstance(miner.get("assigned_atom_ids"), list)
                    else []
                )
                if isinstance(value, str) and value.strip()
            }
            if not evidence_ids.intersection(assigned_ids):
                continue
            miner_tag = _text(miner.get("tag")) or "unidentified_assignment"
            for component, attempts_key, author_role in (
                ("problem_miner", "attempt_history", "problem_miner"),
                (
                    "coverage_review",
                    "coverage_depth_review_attempt_history",
                    "coverage_depth_reviewer",
                ),
            ):
                attempts_raw = miner.get(attempts_key)
                attempts = (
                    [item for item in attempts_raw if isinstance(item, Mapping)]
                    if isinstance(attempts_raw, list)
                    else []
                )
                provenance = _attempt_history_author_provenance(
                    attempts,
                    authoring_stage="problem_mining",
                    author_role=author_role,
                    case_id=_text(problem.get("case_id")),
                    problem_id=_text(problem.get("problem_id")),
                )
                if provenance is None:
                    continue
                _attach_repair_frontier(provenance, repair_frontier)
                component_id = f"{component}:{miner_tag}"
                provenance.update(
                    {
                        "author_component_id": component_id,
                        "stage1_correction_adapter": component,
                        "miner_tag": miner_tag,
                        "assigned_atom_ids": sorted(assigned_ids),
                        "evidence_atom_ids": sorted(evidence_ids),
                    }
                )
                frontiers.append(
                    {
                        "component_id": component_id,
                        "component_kind": component,
                        "author_provenance": provenance,
                    }
                )
        if frontiers:
            coverage_default = next(
                (
                    item["component_id"]
                    for item in frontiers
                    if item["component_kind"] == "coverage_review"
                ),
                frontiers[0]["component_id"],
            )
            bind(
                "problem",
                problem,
                {
                    "provenance_source": "composite_output_component_frontiers",
                    "authoring_stage": "problem_mining",
                    "case_id": _text(problem.get("case_id")),
                    "problem_id": _text(problem.get("problem_id")),
                    "default_author_component_target": coverage_default,
                    "author_component_frontiers": frontiers,
                },
            )

    for relation in accepted_outputs_by_kind.get("relation", []):
        focus_id = _text(relation.get("focus_id"))
        relation_meta, repair_frontier = _retained_repair_author_meta(
            stage1,
            problem_ids={focus_id} if focus_id is not None else set(),
        )
        relation_batches_for_output_raw = relation_meta.get("relation_review_batches")
        relation_batches_for_output = (
            [
                item
                for item in relation_batches_for_output_raw
                if isinstance(item, Mapping)
            ]
            if isinstance(relation_batches_for_output_raw, list)
            else relation_batches
        )
        batch = next(
            (
                item
                for item in relation_batches_for_output
                if focus_id is not None
                and focus_id
                in {
                    value
                    for value in (
                        item.get("focus_ids")
                        if isinstance(item.get("focus_ids"), list)
                        else []
                    )
                    if isinstance(value, str) and value.strip()
                }
            ),
            None,
        )
        if not isinstance(batch, Mapping):
            continue
        attempts_raw = batch.get("attempt_history")
        attempts = (
            [item for item in attempts_raw if isinstance(item, Mapping)]
            if isinstance(attempts_raw, list)
            else []
        )
        provenance = _attempt_history_author_provenance(
            attempts,
            authoring_stage="problem_mining",
            author_role="relation_reviewer",
            case_id=None,
            problem_id=focus_id,
        )
        if provenance is not None:
            _attach_repair_frontier(provenance, repair_frontier)
            provenance.update(
                {
                    "author_component_id": "relation_review:"
                    + str(_text(batch.get("tag")) or "unidentified_batch"),
                    "stage1_correction_adapter": "relation_review",
                    "relation_review_batch_tag": _text(batch.get("tag")),
                    "relation_review_focus_ids": list(batch.get("focus_ids") or []),
                }
            )
        bind("relation", relation, provenance)

    stage2_meta_raw = stage2.get("input_meta")
    stage2_meta = stage2_meta_raw if isinstance(stage2_meta_raw, Mapping) else {}
    priority_attempts_raw = stage2_meta.get("prioritizer_attempt_history")
    priority_attempts = (
        [item for item in priority_attempts_raw if isinstance(item, Mapping)]
        if isinstance(priority_attempts_raw, list)
        else []
    )
    for priority in accepted_outputs_by_kind.get("priority", []):
        priority_problem_id = _text(priority.get("problem_id"))
        priority_meta, repair_frontier = _retained_repair_author_meta(
            stage2,
            problem_ids=(
                {priority_problem_id} if priority_problem_id is not None else set()
            ),
        )
        retained_priority_attempts_raw = priority_meta.get(
            "prioritizer_attempt_history"
        )
        retained_priority_attempts = (
            [
                item
                for item in retained_priority_attempts_raw
                if isinstance(item, Mapping)
            ]
            if isinstance(retained_priority_attempts_raw, list)
            else priority_attempts
        )
        provenance = _attempt_history_author_provenance(
            retained_priority_attempts,
            authoring_stage="problem_prioritization",
            author_role="prioritizer",
            case_id=_text(priority.get("case_id")),
            problem_id=_text(priority.get("problem_id")),
        )
        if provenance is not None:
            _attach_repair_frontier(provenance, repair_frontier)
            provenance.update(
                {
                    "assignment_id": "global_problem_prioritization",
                    "author_component_id": "prioritizer:global_problem_prioritization",
                }
            )
        bind("priority", priority, provenance)

    for research in accepted_outputs_by_kind.get("research", []):
        bind("research", research, _research_author_provenance(research))

    stage4_meta_raw = stage4.get("input_meta")
    stage4_meta = stage4_meta_raw if isinstance(stage4_meta_raw, Mapping) else {}
    option_runs_raw = stage4_meta.get("optioning_correction_runs")
    option_runs = (
        [item for item in option_runs_raw if isinstance(item, Mapping)]
        if isinstance(option_runs_raw, list)
        else []
    )
    for option in accepted_outputs_by_kind.get("option", []):
        problem_id = _text(option.get("problem_id"))
        case_id = _text(option.get("case_id"))
        option_meta, repair_frontier = _retained_repair_author_meta(
            stage4,
            problem_ids={problem_id} if problem_id is not None else set(),
        )
        retained_option_runs_raw = option_meta.get("optioning_correction_runs")
        retained_option_runs = (
            [
                item
                for item in retained_option_runs_raw
                if isinstance(item, Mapping)
            ]
            if isinstance(retained_option_runs_raw, list)
            else option_runs
        )
        run = next(
            (
                item
                for item in reversed(retained_option_runs)
                if _text(item.get("problem_id")) == problem_id
            ),
            None,
        )
        attempts_raw = run.get("attempt_history") if isinstance(run, Mapping) else None
        attempts = (
            [item for item in attempts_raw if isinstance(item, Mapping)]
            if isinstance(attempts_raw, list)
            else []
        )
        option_provenance = _attempt_history_author_provenance(
                attempts,
                authoring_stage="solution_optioning",
                author_role="optioner",
                case_id=case_id,
                problem_id=problem_id,
            )
        bind(
            "option",
            option,
            _attach_repair_frontier(option_provenance, repair_frontier),
        )

    stage5_meta_raw = stage5.get("input_meta")
    stage5_meta = stage5_meta_raw if isinstance(stage5_meta_raw, Mapping) else {}
    outcomes_raw = stage5_meta.get("selection_outcomes")
    outcomes = (
        [item for item in outcomes_raw if isinstance(item, Mapping)]
        if isinstance(outcomes_raw, list)
        else []
    )
    for selection in accepted_outputs_by_kind.get("selection", []):
        problem_id = _text(selection.get("problem_id"))
        case_id = _text(selection.get("case_id"))
        selection_meta, repair_frontier = _retained_repair_author_meta(
            stage5,
            problem_ids={problem_id} if problem_id is not None else set(),
        )
        retained_outcomes_raw = selection_meta.get("selection_outcomes")
        retained_outcomes = (
            [
                item
                for item in retained_outcomes_raw
                if isinstance(item, Mapping)
            ]
            if isinstance(retained_outcomes_raw, list)
            else outcomes
        )
        embedded_raw = selection.get("role_healing")
        embedded = embedded_raw if isinstance(embedded_raw, Mapping) else {}
        candidate_runs_raw = embedded.get("role_runs")
        candidate_runs = (
            [item for item in candidate_runs_raw if isinstance(item, Mapping)]
            if isinstance(candidate_runs_raw, list)
            else []
        )
        if not candidate_runs:
            matching_outcome = next(
                (
                    outcome
                    for outcome in retained_outcomes
                    if _text(outcome.get("problem_id")) == problem_id
                ),
                None,
            )
            runs_raw = (
                matching_outcome.get("role_runs") if isinstance(matching_outcome, Mapping) else None
            )
            candidate_runs = (
                [item for item in runs_raw if isinstance(item, Mapping)]
                if isinstance(runs_raw, list)
                else []
            )
        if not candidate_runs:
            retained_role_runs_raw = selection_meta.get("role_healing_runs")
            candidate_runs = (
                [
                    item
                    for item in retained_role_runs_raw
                    if isinstance(item, Mapping)
                    and _text(item.get("problem_id")) == problem_id
                ]
                if isinstance(retained_role_runs_raw, list)
                else []
            )
        selector = next(
            (run for run in reversed(candidate_runs) if _text(run.get("role")) == "selector"),
            None,
        )
        selection_provenance = (
            _role_run_author_provenance(
                selector,
                authoring_stage="solution_selection",
                case_id=case_id,
                problem_id=problem_id,
            )
            if isinstance(selector, Mapping)
            else None
        )
        bind(
            "selection",
            selection,
            _attach_repair_frontier(selection_provenance, repair_frontier),
        )

    stage6_meta_raw = stage6.get("input_meta")
    stage6_meta = stage6_meta_raw if isinstance(stage6_meta_raw, Mapping) else {}
    planning_runs_raw = stage6_meta.get("planning_correction_runs")
    planning_runs = (
        [item for item in planning_runs_raw if isinstance(item, Mapping)]
        if isinstance(planning_runs_raw, list)
        else []
    )

    def planner_provenance(output: Mapping[str, Any]) -> dict[str, Any] | None:
        case_id = _text(output.get("case_id"))
        problem_id = _text(output.get("problem_id"))
        selected_option_id = _text(output.get("selected_option_id"))
        planner_meta, repair_frontier = _retained_repair_author_meta(
            stage6,
            problem_ids={problem_id} if problem_id is not None else set(),
        )
        retained_planning_runs_raw = planner_meta.get("planning_correction_runs")
        retained_planning_runs = (
            [
                item
                for item in retained_planning_runs_raw
                if isinstance(item, Mapping)
            ]
            if isinstance(retained_planning_runs_raw, list)
            else planning_runs
        )
        run = next(
            (
                item
                for item in reversed(retained_planning_runs)
                if _text(item.get("case_id")) == case_id
                and _text(item.get("problem_id")) == problem_id
                and (
                    selected_option_id is None
                    or _text(item.get("selected_option_id")) == selected_option_id
                )
            ),
            None,
        )
        provenance = (
            _role_run_author_provenance(
                run,
                authoring_stage="implementation_planning",
                case_id=case_id,
                problem_id=problem_id,
            )
            if isinstance(run, Mapping)
            else None
        )
        return _attach_repair_frontier(provenance, repair_frontier)

    plan_provenance_by_revision: dict[str, dict[str, Any]] = {}
    for plan in accepted_outputs_by_kind.get("plan", []):
        provenance = planner_provenance(plan)
        bind("plan", plan, provenance)
        revision_id = _text(plan.get("plan_revision_id"))
        if revision_id is not None and provenance is not None:
            plan_provenance_by_revision[revision_id] = provenance
    for ticket in accepted_outputs_by_kind.get("ticket", []):
        revision_id = _text(ticket.get("plan_revision_id"))
        provenance = plan_provenance_by_revision.get(revision_id or "")
        if provenance is None:
            embedded_plan = ticket.get("change_plan")
            provenance = planner_provenance(
                embedded_plan if isinstance(embedded_plan, Mapping) else ticket
            )
        bind("ticket", ticket, provenance)
    return index


_FALSE_REJECTION_DOWNSTREAM_STAGES: Mapping[str, tuple[str, ...]] = {
    "problem_mining": (
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "problem_prioritization": (
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "repro_research": (
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "solution_optioning": (
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "solution_selection": (
        "solution_selection",
        "implementation_planning",
        "ticket_assembly",
    ),
    "implementation_planning": ("implementation_planning", "ticket_assembly"),
}


def _attempt_history_author_provenance(
    attempts: Sequence[Mapping[str, Any]],
    *,
    authoring_stage: str,
    author_role: str,
    case_id: str | None,
    problem_id: str | None,
) -> dict[str, Any] | None:
    retained = next(
        (attempt for attempt in reversed(attempts) if attempt.get("status") == "verified"),
        attempts[-1] if attempts else None,
    )
    if retained is None:
        return None
    session_id = _text(retained.get("agent_session_id"))
    workspace_dir = _text(retained.get("workspace_dir"))
    workspace_manifest_sha256 = _text(retained.get("workspace_manifest_sha256"))
    observed_sessions = {
        session
        for attempt in attempts
        for session in [_text(attempt.get("agent_session_id"))]
        if session is not None
    }
    observed_workspaces = {
        workspace
        for attempt in attempts
        for workspace in [_text(attempt.get("workspace_dir"))]
        if workspace is not None
    }
    return {
        "provenance_source": "runner_stage_attempt_history",
        "authoring_stage": authoring_stage,
        "author_role": author_role,
        "case_id": case_id,
        "problem_id": problem_id,
        "agent_session_id": session_id,
        "workspace_dir": workspace_dir,
        "repository_revision": _text(retained.get("repo_revision")),
        "workspace_manifest_sha256": workspace_manifest_sha256,
        "exact_session_continuation": bool(
            session_id is not None and observed_sessions == {session_id}
        ),
        "workspace_continuity_verified": bool(
            workspace_dir is not None and observed_workspaces == {workspace_dir}
        ),
        "original_author_cost_seconds": max(
            0.0,
            float(
                attempts[0].get("attempt_elapsed_seconds")
                or attempts[0].get("elapsed_seconds")
                or 0.0
            ),
        ),
        "author_attempt_identity": {
            "attempt_number": retained.get("attempt_number"),
            "attempt_tag": _text(retained.get("attempt_tag")),
            "response_sha256": retained.get("response_sha256"),
            "workspace_manifest_sha256": workspace_manifest_sha256,
        },
        "rerun_downstream_stages": list(
            _FALSE_REJECTION_DOWNSTREAM_STAGES[authoring_stage]
        ),
    }


def _problem_identity_sets(
    records: Sequence[Mapping[str, Any]],
    *,
    atom_ids: set[str],
) -> tuple[set[str], set[str]]:
    case_ids: set[str] = set()
    problem_ids: set[str] = set()
    for record in records:
        record_atom_ids = {
            str(value).strip()
            for field in ("evidence_atom_ids", "supporting_atom_ids")
            for value in (
                record.get(field) if isinstance(record.get(field), list) else []
            )
            if isinstance(value, str) and value.strip()
        }
        if not atom_ids.intersection(record_atom_ids):
            continue
        case_id = _text(record.get("case_id"))
        problem_id = _text(record.get("problem_id"))
        if case_id is not None:
            case_ids.add(case_id)
        if problem_id is not None:
            problem_ids.add(problem_id)
    return case_ids, problem_ids


def _false_rejection_author_provenance(
    *,
    manifest: Any,
    stage1: Mapping[str, Any],
    stage2: Mapping[str, Any],
    stage3: Mapping[str, Any],
    stage4: Mapping[str, Any],
    stage5: Mapping[str, Any],
    stage6: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Locate the deepest retained author frontier for each held-out source group."""

    if not isinstance(manifest, Mapping):
        return {}
    labels_raw = manifest.get("atom_labels")
    labels = (
        [item for item in labels_raw if isinstance(item, Mapping)]
        if isinstance(labels_raw, list)
        else []
    )
    problem_records = _items(dict(stage1))
    result: dict[str, dict[str, Any] | list[dict[str, Any]]] = {}
    for label in labels:
        if _text(label.get("classification")) != "actionable":
            continue
        label_id = _text(label.get("label_id"))
        atom_ids = {
            str(value).strip()
            for value in (
                label.get("atom_ids") if isinstance(label.get("atom_ids"), list) else []
            )
            if isinstance(value, str) and value.strip()
        }
        if label_id is None or not atom_ids:
            continue
        case_ids, problem_ids = _problem_identity_sets(problem_records, atom_ids=atom_ids)

        # Planning is the deepest model-owned stage.  Its run history is retained even
        # when no plan was emitted, making it the correct feedback target for a late miss.
        stage6_meta = stage6.get("input_meta")
        planning_runs_raw = (
            stage6_meta.get("planning_correction_runs")
            if isinstance(stage6_meta, Mapping)
            else None
        )
        planning_runs = (
            [item for item in planning_runs_raw if isinstance(item, Mapping)]
            if isinstance(planning_runs_raw, list)
            else []
        )
        planning_run = next(
            (
                run
                for run in reversed(planning_runs)
                if _text(run.get("case_id")) in case_ids
                or _text(run.get("problem_id")) in problem_ids
            ),
            None,
        )
        if isinstance(planning_run, Mapping):
            provenance = _role_run_author_provenance(
                planning_run,
                authoring_stage="implementation_planning",
                case_id=_text(planning_run.get("case_id")),
                problem_id=_text(planning_run.get("problem_id")),
            )
            if provenance is not None:
                provenance["rerun_downstream_stages"] = list(
                    _FALSE_REJECTION_DOWNSTREAM_STAGES["implementation_planning"]
                )
                result[label_id] = provenance
                continue

        stage5_meta = stage5.get("input_meta")
        role_runs_raw = (
            stage5_meta.get("role_healing_runs")
            if isinstance(stage5_meta, Mapping)
            else None
        )
        role_runs = (
            [item for item in role_runs_raw if isinstance(item, Mapping)]
            if isinstance(role_runs_raw, list)
            else []
        )
        selector_run = next(
            (
                run
                for run in reversed(role_runs)
                if _text(run.get("role")) == "selector"
                and (
                    _text(run.get("case_id")) in case_ids
                    or _text(run.get("problem_id")) in problem_ids
                )
            ),
            None,
        )
        if isinstance(selector_run, Mapping):
            provenance = _role_run_author_provenance(
                selector_run,
                authoring_stage="solution_selection",
                case_id=_text(selector_run.get("case_id")),
                problem_id=_text(selector_run.get("problem_id")),
            )
            if provenance is not None:
                provenance["rerun_downstream_stages"] = list(
                    _FALSE_REJECTION_DOWNSTREAM_STAGES["solution_selection"]
                )
                result[label_id] = provenance
                continue

        stage4_meta = stage4.get("input_meta")
        option_runs_raw = (
            stage4_meta.get("optioning_correction_runs")
            if isinstance(stage4_meta, Mapping)
            else None
        )
        option_runs = (
            [item for item in option_runs_raw if isinstance(item, Mapping)]
            if isinstance(option_runs_raw, list)
            else []
        )
        option_run = next(
            (
                run
                for run in reversed(option_runs)
                if _text(run.get("case_id")) in case_ids
                or _text(run.get("problem_id")) in problem_ids
            ),
            None,
        )
        if isinstance(option_run, Mapping):
            provenance = _role_run_author_provenance(
                option_run,
                authoring_stage="solution_optioning",
                case_id=_text(option_run.get("case_id")),
                problem_id=_text(option_run.get("problem_id")),
            )
            if provenance is not None:
                provenance["rerun_downstream_stages"] = list(
                    _FALSE_REJECTION_DOWNSTREAM_STAGES["solution_optioning"]
                )
                result[label_id] = provenance
                continue

        dossier = next(
            (
                item
                for item in reversed(_items(dict(stage3)))
                if _text(item.get("case_id")) in case_ids
                or _text(item.get("problem_id")) in problem_ids
            ),
            None,
        )
        if isinstance(dossier, Mapping):
            provenance = _research_author_provenance(dossier)
            if provenance is not None:
                provenance["rerun_downstream_stages"] = list(
                    _FALSE_REJECTION_DOWNSTREAM_STAGES["repro_research"]
                )
                result[label_id] = provenance
                continue

        # Stage 2 is one global exact conversation.  It is only selected when Stage 1
        # found a case but no deeper author frontier exists. If the runner-owned full-
        # drain policy already selected the case, the missing Stage 3 output is not a
        # prioritizer mistake; retain an explicit unrouteable research frontier instead
        # of wasting feedback on an author whose decision was correct.
        if problem_ids or case_ids:
            priority_record = next(
                (
                    item
                    for item in _items(dict(stage2))
                    if _text(item.get("case_id")) in case_ids
                    or _text(item.get("problem_id")) in problem_ids
                ),
                None,
            )
            if (
                isinstance(priority_record, Mapping)
                and priority_record.get("selected_for_research") is True
            ):
                result[label_id] = {
                    "provenance_source": "runner_missing_stage_frontier",
                    "authoring_stage": "repro_research",
                    "author_role": "researcher",
                    "case_id": next(iter(sorted(case_ids)), None),
                    "problem_id": next(iter(sorted(problem_ids)), None),
                    "agent_session_id": None,
                    "workspace_dir": None,
                    "exact_session_continuation": False,
                    "workspace_continuity_verified": False,
                    "author_attempt_identity": None,
                    "rerun_downstream_stages": list(
                        _FALSE_REJECTION_DOWNSTREAM_STAGES["repro_research"]
                    ),
                }
                continue
            stage2_meta = stage2.get("input_meta")
            prioritizer_attempts_raw = (
                stage2_meta.get("prioritizer_attempt_history")
                if isinstance(stage2_meta, Mapping)
                else None
            )
            prioritizer_attempts = (
                [item for item in prioritizer_attempts_raw if isinstance(item, Mapping)]
                if isinstance(prioritizer_attempts_raw, list)
                else []
            )
            provenance = _attempt_history_author_provenance(
                prioritizer_attempts,
                authoring_stage="problem_prioritization",
                author_role="prioritizer",
                case_id=next(iter(sorted(case_ids)), None),
                problem_id=next(iter(sorted(problem_ids)), None),
            )
            if provenance is not None:
                result[label_id] = provenance
                continue

        # No problem record survived: route to the independent coverage/depth reviewer
        # for the assignment containing the held-out atoms, falling back to the primary
        # miner only when no reviewer session exists.
        stage1_meta = stage1.get("input_meta")
        stage1_meta_map = stage1_meta if isinstance(stage1_meta, Mapping) else {}
        repair_history_raw = stage1_meta_map.get("qualification_repair_history")
        repair_history = (
            [item for item in repair_history_raw if isinstance(item, Mapping)]
            if isinstance(repair_history_raw, list)
            else []
        )
        candidate_metas = [
            *[
                item.get("replacement_author_input_meta")
                for item in reversed(repair_history)
                if isinstance(item.get("replacement_author_input_meta"), Mapping)
            ],
            stage1_meta_map,
        ]
        matched_miners: list[Mapping[str, Any]] = []
        seen_assignments: set[str] = set()
        for candidate_meta in candidate_metas:
            miner_results_raw = candidate_meta.get("miner_results")
            miner_results = (
                [item for item in miner_results_raw if isinstance(item, Mapping)]
                if isinstance(miner_results_raw, list)
                else []
            )
            for miner in miner_results:
                assigned_ids = {
                    str(value).strip()
                    for value in (
                        miner.get("assigned_atom_ids")
                        if isinstance(miner.get("assigned_atom_ids"), list)
                        else []
                    )
                    if isinstance(value, str) and value.strip()
                }
                if not atom_ids.intersection(assigned_ids):
                    continue
                assignment_key = (
                    _text(miner.get("tag"))
                    or _text(miner.get("assignment_id"))
                    or _canonical_hash(sorted(assigned_ids))
                )
                if assignment_key in seen_assignments:
                    continue
                seen_assignments.add(assignment_key)
                matched_miners.append(miner)
        provenances: list[dict[str, Any]] = []
        covered_atom_ids: set[str] = set()
        for miner in matched_miners:
            assigned_ids = {
                str(value).strip()
                for value in (
                    miner.get("assigned_atom_ids")
                    if isinstance(miner.get("assigned_atom_ids"), list)
                    else []
                )
                if isinstance(value, str) and value.strip()
            }
            targeted_ids = atom_ids.intersection(assigned_ids)
            review_attempts_raw = miner.get("coverage_depth_review_attempt_history")
            primary_attempts_raw = miner.get("attempt_history")
            attempts_raw = (
                review_attempts_raw
                if isinstance(review_attempts_raw, list) and review_attempts_raw
                else primary_attempts_raw
            )
            attempts = (
                [item for item in attempts_raw if isinstance(item, Mapping)]
                if isinstance(attempts_raw, list)
                else []
            )
            provenance = _attempt_history_author_provenance(
                attempts,
                authoring_stage="problem_mining",
                author_role=(
                    "coverage_depth_reviewer"
                    if attempts_raw is review_attempts_raw
                    else "problem_miner"
                ),
                case_id=None,
                problem_id=None,
            )
            component = (
                "coverage_review"
                if attempts_raw is review_attempts_raw
                else "problem_miner"
            )
            miner_tag = _text(miner.get("tag")) or "unidentified_assignment"
            if provenance is None:
                provenance = {
                    "provenance_source": "stage1_assignment_without_resumable_author",
                    "authoring_stage": "problem_mining",
                    "author_role": (
                        "coverage_depth_reviewer"
                        if component == "coverage_review"
                        else "problem_miner"
                    ),
                    "agent_session_id": None,
                    "workspace_dir": None,
                    "exact_session_continuation": False,
                    "workspace_continuity_verified": False,
                    "author_attempt_identity": None,
                }
            provenance.update(
                {
                    "author_component_id": f"{component}:{miner_tag}",
                    "stage1_correction_adapter": component,
                    "miner_tag": miner_tag,
                    "assigned_atom_ids": sorted(assigned_ids),
                    "evidence_atom_ids": sorted(targeted_ids),
                    "causal_target": {
                        "problem_ids": [],
                        "case_ids": [],
                        "evidence_atom_ids": sorted(targeted_ids),
                        "actionable_label_ids": [label_id],
                        "expected_item_keys": [
                            "atom:" + atom_id for atom_id in sorted(targeted_ids)
                        ],
                    },
                }
            )
            provenances.append(provenance)
            covered_atom_ids.update(targeted_ids)
        uncovered_atom_ids = atom_ids - covered_atom_ids
        if uncovered_atom_ids:
            provenances.append(
                {
                    "provenance_source": "stage1_assignment_unavailable",
                    "authoring_stage": "problem_mining",
                    "author_role": "problem_miner",
                    "agent_session_id": None,
                    "workspace_dir": None,
                    "exact_session_continuation": False,
                    "workspace_continuity_verified": False,
                    "author_attempt_identity": None,
                    "author_component_id": "problem_miner:unassigned",
                    "stage1_correction_adapter": "problem_miner",
                    "miner_tag": "unassigned",
                    "assigned_atom_ids": [],
                    "evidence_atom_ids": sorted(uncovered_atom_ids),
                    "causal_target": {
                        "problem_ids": [],
                        "case_ids": [],
                        "evidence_atom_ids": sorted(uncovered_atom_ids),
                        "actionable_label_ids": [label_id],
                        "expected_item_keys": [
                            "atom:" + atom_id
                            for atom_id in sorted(uncovered_atom_ids)
                        ],
                    },
                }
            )
        if provenances:
            result[label_id] = provenances[0] if len(provenances) == 1 else provenances
    return result


def _model_produced_evidence_sufficient_proof(record: Mapping[str, Any]) -> bool:
    """Require a retained valid model attempt, not a runner-synthesized success."""

    if _text(record.get("research_status")) != "evidence_sufficient":
        return False
    attempts_raw = record.get("research_attempts")
    attempts = (
        [item for item in attempts_raw if isinstance(item, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    if not attempts:
        return False
    final_attempt = attempts[-1]
    attempted_raw = final_attempt.get("attempted_dossier")
    attempted = dict(attempted_raw) if isinstance(attempted_raw, Mapping) else None
    retained_claims = {key: value for key, value in record.items() if key != "research_attempts"}
    # These values are injected/verified by the runner after the model response.
    # Neutralize them for the model-claim comparison while the persisted evidence
    # verifier continues to bind their authoritative final values separately.
    for runner_owned_claim in (
        "research_schema_version",
        "repo_revision",
        "diff_classification",
        "evidence_assignment",
    ):
        retained_claims[runner_owned_claim] = (
            attempted.get(runner_owned_claim) if attempted is not None else None
        )
    attempted_artifact_refs = attempted.get("artifact_refs") if attempted is not None else None
    retained_artifact_refs = retained_claims.get("artifact_refs")
    if isinstance(attempted_artifact_refs, list) and isinstance(retained_artifact_refs, list):
        retained_ref_hashes = {_canonical_hash(item) for item in retained_artifact_refs}
        if any(
            _canonical_hash(item) not in retained_ref_hashes for item in attempted_artifact_refs
        ):
            return False
        # The runner appends content-addressed execution receipts after the model
        # attempt. They are proof *of* the claims, not new model-authored claims.
        retained_claims["artifact_refs"] = attempted_artifact_refs
    outcome = _text(final_attempt.get("outcome"))
    if (
        attempted is None
        or outcome not in {"output_contract_valid", "repair_contract_valid"}
        or _text(attempted.get("research_status")) != "evidence_sufficient"
        or final_attempt.get("attempted_dossier_sha256") != _canonical_hash(attempted)
        # Attempt history is immutable provenance *about* the model claims. The
        # stage-contract hash can bind that history when verifying a complete
        # dossier, but it must not make the retained claims differ from the exact
        # final model attempt merely because the runner attached the history.
        or research_claims_sha256(attempted) != research_claims_sha256(retained_claims)
    ):
        return False
    if outcome == "repair_contract_valid":
        session_id = _text(final_attempt.get("agent_session_id"))
        progress_raw = final_attempt.get("repair_progress")
        progress = progress_raw if isinstance(progress_raw, Mapping) else {}
        if (
            _text(final_attempt.get("attempt_kind")) != "model_output_repair"
            or session_id is None
            or _text(final_attempt.get("observed_agent_session_id")) != session_id
            or _text(final_attempt.get("resumed_from_session_id")) != session_id
            or final_attempt.get("validation_errors") != []
            or _text(progress.get("decision")) != "accepted"
        ):
            return False
    return True


def _research_blocker_signals(record: Mapping[str, Any]) -> list[str]:
    """Collect machine-authored research failure signals without mining case prose."""

    signals: list[str] = []

    def add_list(value: Any) -> None:
        if isinstance(value, list):
            signals.extend(text for item in value if (text := _text(item)) is not None)

    add_list(record.get("blocking_reasons"))
    add_list(record.get("runner_report_validation_errors"))
    for field in ("evidence_assignment", "evidence_verification"):
        value = record.get(field)
        if isinstance(value, Mapping):
            add_list(value.get("errors"))
    attempts_raw = record.get("research_attempts")
    attempts = (
        [item for item in attempts_raw if isinstance(item, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    # Attempt history is repair telemetry. Only the terminal retained attempt can
    # describe the dossier's current blocker; superseded parse/contract failures do
    # not turn a successfully repaired or honestly downgraded dossier into a systemic
    # failure.
    if attempts:
        terminal = attempts[-1]
        outcome = _text(terminal.get("outcome"))
        if outcome not in {"output_contract_valid", "repair_contract_valid"}:
            if outcome is not None:
                signals.append(f"attempt_outcome:{outcome}")
            add_list(terminal.get("validation_errors"))
    return list(dict.fromkeys(signals))


def _systemic_research_blocker_code(
    record: Mapping[str, Any],
    *,
    retained_validation_errors: list[str] | None = None,
) -> str | None:
    """Classify internal research infrastructure failures, never evidence scarcity."""

    retained_errors = retained_validation_errors or []
    if not isinstance(record, dict) or (
        not _has_explicit_research_block(record) and not retained_errors
    ):
        return None
    normalized = [
        signal.casefold() for signal in [*_research_blocker_signals(record), *retained_errors]
    ]
    if any(
        fragment in signal
        for signal in normalized
        for fragment in (
            "codex_execpolicy_chatgpt_login_status_failed",
            "codex_execpolicy_post_chatgpt_login_status_failed",
            "chatgpt_subscription_auth",
            "authentication_failed",
            "login_status_failed",
            "not_logged_in",
            "refresh_token",
        )
    ):
        return "auth"
    if any(
        signal.startswith(prefix)
        for signal in normalized
        for prefix in (
            "research_attempt_artifact_missing:",
            "research_attempt_artifact_changed:",
            "research_artifact_changed:",
            "research_output_contract_retry_result_missing",
            "research_report_missing",
            "research_runner_artifact_changed:",
            "research_target_ref_artifact_missing",
            "runner_repo_revision_unavailable",
        )
    ):
        return "produced_artifact_loss"
    if any(
        signal.startswith(prefix)
        for signal in normalized
        for prefix in (
            "attempt_outcome:invocation_failed",
            "research_runner_exception:",
            "runner_exit_code:",
        )
    ):
        return "invocation"
    if any(
        signal.startswith(prefix)
        for signal in normalized
        for prefix in (
            "attempt_outcome:runner_contract_invalid",
            "attempt_outcome:output_contract_invalid",
            "research_dossier_output_contract_invalid",
            "research_dossier_malformed:",
            "research_extension_missing:",
            "research_report_malformed:",
            "research_report_schema_invalid:",
            "research_output_contract_retry_failed",
            "research_output_contract_retry_not_fresh",
            "research_output_contract_retry_revision_changed",
            "research_output_contract_retry_freshness_unverifiable",
            "research_implementation_performed_forbidden",
            "runner_report_validation_errors",
            "suspicious_implementation_diff",
        )
    ):
        return "runner_contract"
    return None


def _persisted_plan_grounding_errors(plan: Mapping[str, Any]) -> list[str]:
    """Use the runner-authored v2/v3 target contract as grounding authority."""

    errors: list[str] = []
    revision_id = _text(plan.get("plan_revision_id"))
    if revision_id is None:
        errors.append("plan_revision_id_missing")
    elif revision_id != plan_revision_id_for(plan):
        errors.append("plan_revision_id_content_mismatch")
    if plan.get("plan_revision_source") != "server_content_addressed_v1":
        errors.append("plan_revision_source_invalid")
    contract_raw = plan.get("target_contract")
    try:
        contract = validate_plan_target_contract(contract_raw)
    except ValueError as exc:
        errors.append(str(exc))
        contract = None
    if contract is not None:
        for field in ("case_id", "problem_id", "selected_option_id", "repo_revision"):
            if contract.get(field) != plan.get(field):
                errors.append(f"plan_target_contract_{field}_mismatch")

        def target_projection(raw: Any, *, schema_version: int) -> list[dict[str, Any]]:
            if not isinstance(raw, list):
                return []
            return [
                {
                    "action": target.get("action"),
                    "path": target.get("path"),
                    "destination_path": (
                        target.get("destination_path") if schema_version >= 3 else None
                    ),
                    # Symbols are optional in v3 for whole-file operations such as
                    # create/delete/move; absence and an empty list are equivalent.
                    "symbols": target.get("symbols", []),
                    "change": target.get("change"),
                }
                for target in raw
                if isinstance(target, Mapping)
            ]

        contract_targets = contract.get("targets")
        plan_targets = plan.get("change_targets")
        schema_version = int(contract["schema_version"])
        contract_projection = target_projection(
            contract_targets,
            schema_version=schema_version,
        )
        plan_projection = target_projection(
            plan_targets,
            schema_version=schema_version,
        )
        if (
            not isinstance(plan_targets, list)
            or len(plan_projection) != len(plan_targets)
            or contract_projection != plan_projection
        ):
            errors.append("plan_target_contract_targets_mismatch")
    commands_raw = plan.get("verification_commands")
    commands = commands_raw if isinstance(commands_raw, list) else []
    if not commands or any(_text(command) is None for command in commands):
        errors.append("verification_commands_missing")
    return errors


def _actionable_nonterminal_case_ids(
    stage2: Mapping[str, Any],
    case_registry: Mapping[str, Any],
) -> set[str]:
    """Return selected active cases; exhausted terminal corpora need no new output."""

    cases_raw = case_registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, Mapping) else {}
    problem_map_raw = case_registry.get("problem_id_to_case_id")
    problem_map = problem_map_raw if isinstance(problem_map_raw, Mapping) else {}

    def canonical(case_id: str) -> str:
        seen: set[str] = set()
        while case_id not in seen:
            seen.add(case_id)
            record = cases.get(case_id)
            if not isinstance(record, Mapping) or _text(record.get("state")) != "alias":
                break
            target = _text(record.get("alias_of"))
            if target is None:
                break
            case_id = target
        return case_id

    result: set[str] = set()
    for priority in _items(dict(stage2)):
        if priority.get("selected_for_research") is not True:
            continue
        case_id = _text(priority.get("case_id"))
        if case_id is None:
            problem_id = _text(priority.get("problem_id"))
            case_id = _text(problem_map.get(problem_id or ""))
        if case_id is None:
            continue
        case_id = canonical(case_id)
        case = cases.get(case_id)
        state = _text(case.get("state")) or "active" if isinstance(case, Mapping) else "active"
        if state not in _CASE_TERMINAL_OUTCOMES and state != "alias":
            result.add(case_id)
    return result


def _is_meaningful_automated_observation(atom: Mapping[str, Any]) -> bool:
    """Exclude external ideas, derived commentary, and proposal-only evidence."""

    if not atom_is_independent_problem_evidence(atom):
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
            and _text(atom.get("disposition")) in {"unresolved", "novel_case"}
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
            evidence_bound_no_change = any(
                _text(outcome.get("optioning_status")) == "not_required"
                and _text(outcome.get("research_actionability_disposition"))
                in {"already_addressed", "non_actionable"}
                and any(
                    isinstance(research_item.get("actionability_assessment"), Mapping)
                    and _text(research_item["actionability_assessment"].get("disposition"))
                    == _text(outcome.get("research_actionability_disposition"))
                    and _clean_string_set(
                        research_item["actionability_assessment"].get("evidence_refs")
                    )
                    == _clean_string_set(outcome.get("evidence_refs"))
                    and bool(_clean_string_set(outcome.get("evidence_refs")))
                    for research_item in research
                )
                for outcome in option_outcomes
            )
            if evidence_bound_no_change:
                continue
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


def _source_atom_corpus_projection(
    atoms: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project source evidence without model/lifecycle decisions.

    Qualification stability asks whether the observed evidence stayed the same,
    not whether two nondeterministic miners chose byte-identical disposition or
    canonical-case fields. Process audit timestamps are likewise excluded by the
    shared immutable projection.
    """

    projected = [immutable_atom_evidence_projection(atom) for atom in atoms]
    return sorted(
        projected,
        key=lambda atom: (
            _text(atom.get("atom_id")) or "",
            _canonical_hash(atom),
        ),
    )


def qualification_accepted_outputs(
    *,
    backlog: Mapping[str, Any],
    stage1: Mapping[str, Any],
    stage2: Mapping[str, Any],
    stage3: Mapping[str, Any],
    stage4: Mapping[str, Any],
    stage5: Mapping[str, Any],
    stage6: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Return the exact authoritative output corpus rated by qualification.

    This projection is deliberately public and side-effect free. The phase-two
    scorer and the independent adjudication-template command share it so operators
    never have to recreate hidden readiness, research, or plan-grounding filters.
    """

    documents = {
        "stage1": dict(stage1),
        "stage2": dict(stage2),
        "stage3": dict(stage3),
        "stage4": dict(stage4),
        "stage5": dict(stage5),
        "stage6": dict(stage6),
    }
    research_by_identity: dict[str, bool] = {}
    model_ready_research: list[dict[str, Any]] = []
    model_ready_identities: set[str] = set()
    for dossier in _items(documents["stage3"]):
        ready, _ = assess_research_readiness(dossier)
        retained_ready, _ = verify_persisted_research_evidence(dossier)
        ready = ready and retained_ready
        model_ready = ready and _model_produced_evidence_sufficient_proof(dossier)
        if model_ready:
            model_ready_research.append(dossier)
        for identity in (_text(dossier.get("case_id")), _text(dossier.get("problem_id"))):
            if identity is None:
                continue
            research_by_identity[identity] = research_by_identity.get(identity, False) or ready
            if model_ready:
                model_ready_identities.add(identity)

    plans_by_revision: dict[str, dict[str, Any]] = {}
    grounded_plans: list[dict[str, Any]] = []
    for plan in _items(documents["stage6"]):
        revision_id = _text(plan.get("plan_revision_id"))
        if revision_id is None:
            continue
        plans_by_revision[revision_id] = plan
        if not _persisted_plan_grounding_errors(plan):
            grounded_plans.append(plan)

    tickets_raw = backlog.get("tickets")
    tickets = (
        [dict(item) for item in tickets_raw if isinstance(item, Mapping)]
        if isinstance(tickets_raw, list)
        else []
    )
    authoritative_tickets: list[dict[str, Any]] = []
    for ticket in tickets:
        if _text(ticket.get("stage")) != "ready_for_ticket":
            continue
        ready, _ = assess_ticket_readiness(ticket)
        identity = _text(ticket.get("case_id")) or _text(ticket.get("problem_id"))
        revision_id = _text(ticket.get("plan_revision_id"))
        embedded_plan = ticket.get("change_plan")
        embedded_revision_id = (
            _text(embedded_plan.get("plan_revision_id"))
            if isinstance(embedded_plan, Mapping)
            else None
        )
        persisted_plan = plans_by_revision.get(revision_id or "")
        grounding_errors = (
            _persisted_plan_grounding_errors(persisted_plan)
            if persisted_plan is not None
            else ["persisted_plan_missing"]
        )
        if (
            ready
            and identity is not None
            and research_by_identity.get(identity, False)
            and identity in model_ready_identities
            and revision_id is not None
            and revision_id == embedded_revision_id
            and persisted_plan is not None
            and not grounding_errors
        ):
            authoritative_tickets.append(ticket)

    stage1_meta = documents["stage1"].get("input_meta")
    relation_raw = (
        stage1_meta.get("relation_review_decisions")
        if isinstance(stage1_meta, Mapping)
        else None
    )
    relations = (
        [dict(item) for item in relation_raw if isinstance(item, Mapping)]
        if isinstance(relation_raw, list)
        else []
    )
    return {
        "problem": _items(documents["stage1"]),
        "relation": relations,
        "priority": _items(documents["stage2"]),
        "research": model_ready_research,
        "option": _items(documents["stage4"]),
        "selection": _items(documents["stage5"]),
        "plan": grounded_plans,
        "ticket": authoritative_tickets,
    }


def _sorted_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item.strip() for item in value if isinstance(item, str) and item.strip()})


_CASE_LOCAL_CAUSAL_FIELDS = frozenset(
    {
        "attempt_id",
        "baseline_experiment_id",
        "baseline_selection_id",
        "causal_control_ids",
        "challenge_experiment_id",
        "challenge_selection_id",
        "claim",
        "closure_receipt_id",
        "control_experiment_id",
        "control_selection_id",
        "control_verification_id",
        "controlled_variable",
        "deterministic_closure_ids",
        "expected_difference",
        "experiment_id",
        "failure_path_id",
        "falsification_intervention_ids",
        "hypothesis_id",
        "intervention_receipt_id",
        "mechanism_evidence_id",
        "mechanism_evidence_ids",
        "primary_hypothesis_id",
        "relationship_sha256",
        "selection_id",
        "support_experiment_id",
        "support_selection_id",
        "supports_experiment_id",
    }
)


def _runner_causal_value(value: Any) -> Any:
    """Remove model labels while retaining runner-observed causal content."""

    if isinstance(value, Mapping):
        return {
            str(key): _runner_causal_value(item)
            for key, item in value.items()
            if str(key) not in _CASE_LOCAL_CAUSAL_FIELDS
        }
    if isinstance(value, list):
        projected = [_runner_causal_value(item) for item in value]
        if all(isinstance(item, Mapping) for item in projected):
            return sorted(projected, key=_canonical_hash)
        return projected
    return value


def _runner_causal_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records = [
        projected
        for item in value
        if isinstance(item, Mapping)
        for projected in [_runner_causal_value(item)]
        if isinstance(projected, dict)
    ]
    return sorted(records, key=_canonical_hash)


def _atom_binding_proof_basis(value: Any, *, receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, Mapping):
            continue
        binding = {
            key: raw.get(key)
            for key in (
                "atom_id",
                "match_kind",
                "origin_atom_field_path",
                "origin_atom_value_sha256",
                "origin_atom_sha256",
                "origin_artifact_sha256",
            )
            if raw.get(key) is not None
        }
        artifact_path = _stable_research_path(raw.get("origin_artifact_path"), receipt=receipt)
        if artifact_path is not None and raw.get("origin_artifact_sha256") is None:
            binding["origin_artifact_path"] = artifact_path
        projected.append(binding)
    return sorted(projected, key=_canonical_hash)


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
        "expected_atom_ids": _sorted_strings(assignment.get("expected_atom_ids")),
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
            "baseline_state_sha256",
            "baseline_git_index_sha256",
            "git_index_changed",
        )
    } | {
        "changed_baseline_paths": _sorted_strings(overlay.get("changed_baseline_paths")),
        "suspicious_extra_paths": _sorted_strings(overlay.get("suspicious_extra_paths")),
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
        "executed_argv": [
            _artifact_reference_proof_basis(item, aliases=artifact_aliases) or item
            for item in (
                experiment.get("executed_argv", [])
                if isinstance(experiment.get("executed_argv"), list)
                else []
            )
        ],
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
        overlay_raw = receipt.get("workspace_overlay")
        overlay = overlay_raw if isinstance(overlay_raw, Mapping) else {}
        overlay_manifest_raw = overlay.get("research_overlay_manifest")
        overlay_manifest = overlay_manifest_raw if isinstance(overlay_manifest_raw, Mapping) else {}
        for path, raw_entry in overlay_manifest.items():
            entry = raw_entry if isinstance(raw_entry, Mapping) else {}
            digest = _text(entry.get("sha256"))
            if isinstance(path, str) and digest is not None:
                artifact_aliases[path.replace("\\", "/").casefold()] = f"overlay_sha256:{digest}"
        for raw_artifact in receipt.get("artifacts", []):
            if not isinstance(raw_artifact, Mapping):
                continue
            artifact = dict(raw_artifact)
            artifact_id = _text(artifact.get("artifact_id"))
            artifact_sha256 = _text(artifact.get("sha256"))
            stable_artifact_id = (
                f"artifact_sha256:{artifact_sha256}" if artifact_sha256 is not None else artifact_id
            )
            for alias in (
                artifact_id,
                _text(artifact.get("declared_path")),
                _text(artifact.get("path")),
            ):
                if alias is not None and stable_artifact_id is not None:
                    artifact_aliases[alias.replace("\\", "/").casefold()] = stable_artifact_id
            artifacts.append(
                {
                    "artifact_identity": stable_artifact_id,
                    "kind": _text(artifact.get("kind")),
                    "sha256": artifact_sha256,
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
        verified_mechanism_sha256 = _text(receipt.get("verified_mechanism_sha256"))
        causal_evidence = (
            verified_causal_evidence_projection(
                dossier,
                verified_mechanism_sha256=verified_mechanism_sha256,
            )
            if verified_mechanism_sha256 is not None
            else None
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
                    "artifacts": sorted(artifacts, key=_canonical_hash),
                    "experiments": sorted(experiments, key=_canonical_hash),
                    "inspected_files": sorted(inspected_files, key=_canonical_hash),
                    "inspected_symbols": sorted(inspected_symbols, key=_canonical_hash),
                    "verified_mechanism_sha256": verified_mechanism_sha256,
                    "verified_mechanism": _runner_causal_value(receipt.get("verified_mechanism")),
                    "verified_causal_evidence": causal_evidence,
                    "causal_links": _runner_causal_records(receipt.get("causal_links")),
                    "test_selections": _runner_causal_records(receipt.get("test_selections")),
                    "control_verifications": _runner_causal_records(
                        receipt.get("control_verifications")
                    ),
                    "falsification_interventions": _runner_causal_records(
                        receipt.get("falsification_interventions")
                    ),
                    "deterministic_mechanism_closures": _runner_causal_records(
                        receipt.get("deterministic_mechanism_closures")
                    ),
                    "failure_paths": _runner_causal_records(receipt.get("failure_paths")),
                    "atom_bindings": _atom_binding_proof_basis(
                        receipt.get("atom_bindings"),
                        receipt=receipt,
                    ),
                    "outcome_oracles": _runner_causal_records(receipt.get("outcome_oracles")),
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
    qualification_contract: Mapping[str, Any] | None = None,
    qualification_manifest: Any = None,
    qualification_manifest_sha256_expected: str | None = None,
    qualification_manifest_sha256_observed: str | None = None,
    qualification_output_adjudication: Any = None,
    qualification_output_adjudication_sha256_pre_run: str | None = None,
    qualification_output_adjudication_sha256_post_run: str | None = None,
    qualification_pending_run_sha256: str | None = None,
    no_actionable_evidence_receipt: Any = None,
    cycle_mode: str = "release",
) -> dict[str, Any]:
    """Evaluate one complete non-exporting cycle.

    ``release`` cycles include sealed, independent outcome adjudication and may
    establish a reusable pipeline/config qualification anchor. ``operational``
    cycles evaluate the same causal, lineage, grounding, and provenance contracts
    against fresh production artifacts, but deliberately do not claim independent
    qualification. This keeps routine throughput available without letting routine
    runs silently certify their own quality.
    """
    if cycle_mode not in {"release", "operational"}:
        raise ValueError(f"shadow_cycle_mode_invalid:{cycle_mode}")
    qualification = _shadow_qualification_contract(qualification_contract)
    failures: list[str] = []
    backlog_meta_raw = backlog.get("input_meta")
    backlog_meta = backlog_meta_raw if isinstance(backlog_meta_raw, Mapping) else {}
    codex_invocation_provenance_required = bool(
        _text(backlog_meta.get("agent")) == "codex" and backlog_meta.get("dry_run") is not True
    )
    for stage_doc in (stage1, stage2, stage4, stage5, stage6):
        stage_meta_raw = stage_doc.get("input_meta")
        stage_meta = stage_meta_raw if isinstance(stage_meta_raw, Mapping) else {}
        has_contract = isinstance(stage_meta.get("model_invocation_contract"), Mapping)
        if not codex_invocation_provenance_required and not has_contract:
            continue
        failures.extend(
            "stage_model_invocation_provenance_invalid:"
            + (_text(stage_doc.get("stage")) or "unknown")
            + ":"
            + error
            for error in verify_stage_model_invocation_contract(stage_doc)
        )
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
    actionable_nonterminal_cases = _actionable_nonterminal_case_ids(stage2, case_registry)
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
    model_produced_ready_research_proofs: list[dict[str, Any]] = []
    model_produced_ready_research_identities: set[str] = set()
    honest_case_specific_blocked_research: list[dict[str, Any]] = []
    systemic_research_blockers: list[tuple[str, str]] = []
    for dossier in _items(stage3):
        ready, reasons = assess_research_readiness(dossier)
        retained_ready, retained_reasons = verify_persisted_research_evidence(dossier)
        verification = dossier.get("evidence_verification")
        claimed_retained_proof = _text(dossier.get("research_status")) == "evidence_sufficient" or (
            isinstance(verification, Mapping) and _text(verification.get("status")) == "verified"
        )
        blocker_code = _systemic_research_blocker_code(
            dossier,
            retained_validation_errors=(
                retained_reasons if claimed_retained_proof and not retained_ready else []
            ),
        )
        if blocker_code is not None:
            identity = _text(dossier.get("case_id")) or _text(dossier.get("problem_id"))
            systemic_research_blockers.append((identity or "(missing)", blocker_code))
        elif _has_explicit_research_block(dossier):
            honest_case_specific_blocked_research.append(dossier)
        if not retained_ready:
            ready = False
            reasons = [
                *reasons,
                "retained_research_evidence_invalid",
                *[f"retained:{reason}" for reason in retained_reasons],
            ]
        if ready:
            ready_research_proofs.append(dossier)
            if _model_produced_evidence_sufficient_proof(dossier):
                model_produced_ready_research_proofs.append(dossier)
                model_produced_ready_research_identities.update(
                    identity
                    for identity in (
                        _text(dossier.get("case_id")),
                        _text(dossier.get("problem_id")),
                    )
                    if identity is not None
                )
            elif _text(dossier.get("research_status")) == "evidence_sufficient":
                failures.append(
                    "evidence_sufficient_research_not_model_produced:"
                    f"{_text(dossier.get('case_id')) or _text(dossier.get('problem_id'))}"
                )
        for identity in (_text(dossier.get("case_id")), _text(dossier.get("problem_id"))):
            if identity is not None:
                previous = research_by_identity.get(identity)
                research_by_identity[identity] = (
                    ready or bool(previous and previous[0]),
                    list(dict.fromkeys([*(previous[1] if previous else []), *reasons])),
                )

    plans_by_revision: dict[str, dict[str, Any]] = {}
    code_grounded_plan_revisions: set[str] = set()
    for plan in _items(stage6):
        revision_id = _text(plan.get("plan_revision_id"))
        if revision_id is None:
            continue
        plans_by_revision[revision_id] = plan
        if not _persisted_plan_grounding_errors(plan):
            code_grounded_plan_revisions.add(revision_id)

    tickets_raw = backlog.get("tickets")
    tickets = (
        [item for item in tickets_raw if isinstance(item, dict)]
        if isinstance(tickets_raw, list)
        else []
    )
    authoritative_ready_tickets: list[dict[str, Any]] = []
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
        if identity not in model_produced_ready_research_identities:
            failures.append(f"ready_ticket_without_model_produced_research:{identity}")
        revision_id = _text(ticket.get("plan_revision_id"))
        embedded_plan = ticket.get("change_plan")
        embedded_revision_id = (
            _text(embedded_plan.get("plan_revision_id"))
            if isinstance(embedded_plan, Mapping)
            else None
        )
        plan_revision_linked = revision_id is not None and revision_id == embedded_revision_id
        if not plan_revision_linked:
            failures.append(
                "ready_ticket_plan_revision_linkage_mismatch:"
                f"ticket={revision_id}:embedded={embedded_revision_id}"
            )
        persisted_plan = plans_by_revision.get(revision_id or "")
        if persisted_plan is None:
            failures.append(f"ready_ticket_missing_persisted_plan:{revision_id}")
            grounding_errors = ["persisted_plan_missing"]
        else:
            grounding_errors = _persisted_plan_grounding_errors(persisted_plan)
            if grounding_errors:
                failures.append(
                    "ready_ticket_persisted_plan_not_code_grounded:"
                    f"{revision_id}:" + ",".join(grounding_errors)
                )
        if (
            ready
            and research is not None
            and research[0]
            and identity in model_produced_ready_research_identities
            and plan_revision_linked
            and persisted_plan is not None
            and not grounding_errors
        ):
            authoritative_ready_tickets.append(ticket)

    qualification_outputs = qualification_accepted_outputs(
        backlog=backlog,
        stage1=stage1,
        stage2=stage2,
        stage3=stage3,
        stage4=stage4,
        stage5=stage5,
        stage6=stage6,
    )
    output_author_provenance = _qualification_output_author_provenance(
        stage1=stage1,
        stage2=stage2,
        stage3=stage3,
        stage4=stage4,
        stage5=stage5,
        stage6=stage6,
        accepted_outputs_by_kind=qualification_outputs,
    )
    false_rejection_author_provenance = _false_rejection_author_provenance(
        manifest=qualification_manifest,
        stage1=stage1,
        stage2=stage2,
        stage3=stage3,
        stage4=stage4,
        stage5=stage5,
        stage6=stage6,
    )
    backlog_artifacts_raw = backlog.get("artifacts")
    backlog_artifacts = (
        backlog_artifacts_raw if isinstance(backlog_artifacts_raw, Mapping) else {}
    )
    shadow_qualification_raw = backlog_artifacts.get("shadow_qualification")
    shadow_qualification = (
        shadow_qualification_raw
        if isinstance(shadow_qualification_raw, Mapping)
        else {}
    )
    independent_qualification = evaluate_independent_qualification(
        atoms=atoms,
        accepted_outputs_by_kind=qualification_outputs,
        manifest=qualification_manifest,
        qualification_manifest_sha256_expected=qualification_manifest_sha256_expected,
        qualification_manifest_sha256_observed=qualification_manifest_sha256_observed,
        output_adjudication=qualification_output_adjudication,
        output_adjudication_sha256_pre_run=(qualification_output_adjudication_sha256_pre_run),
        output_adjudication_sha256_post_run=(qualification_output_adjudication_sha256_post_run),
        pending_run_sha256=qualification_pending_run_sha256,
        no_actionable_receipt=no_actionable_evidence_receipt,
        output_author_provenance=output_author_provenance,
        false_rejection_author_provenance=false_rejection_author_provenance,
        same_corpus_feedback_exposed=bool(
            shadow_qualification.get("same_corpus_feedback_exposed") is True
        ),
        correction_metrics=(
            shadow_qualification.get("correction_metrics")
            if isinstance(
                shadow_qualification.get("correction_metrics"),
                Mapping,
            )
            else None
        ),
        source_adjudication_sha256_expected=_text(
            shadow_qualification.get(
                "source_qualification_output_adjudication_sha256"
            )
        ),
        source_correction_findings_expected=(
            [
                dict(item)
                for item in shadow_qualification.get("source_correction_findings", [])
                if isinstance(item, Mapping)
            ]
            if isinstance(
                shadow_qualification.get("source_correction_findings"),
                list,
            )
            else None
        ),
        positive_throughput_required=qualification["require_nonempty_throughput"],
        minimum_good_ticket_count=qualification["minimum_good_ticket_count"],
        minimum_good_to_bad_ratio=qualification["minimum_good_to_bad_ratio"],
        minimum_recovered_to_missed_ratio=qualification["minimum_recovered_to_missed_ratio"],
        require_zero_unknown_authoritative_tickets=qualification[
            "require_zero_unknown_authoritative_tickets"
        ],
    )
    if cycle_mode == "release":
        failures.extend(independent_qualification["failures"])
    if independent_qualification.get("qualification_class") == "verified_exhaustion" and tickets:
        failures.append(f"shadow_verified_exhaustion_backlog_not_empty:ticket_count={len(tickets)}")
    independent_counts_raw = independent_qualification.get("counts")
    independent_counts = (
        independent_counts_raw if isinstance(independent_counts_raw, Mapping) else {}
    )
    independent_actionable_count = independent_counts.get("actionable_cases")
    independently_actionable = (
        isinstance(independent_actionable_count, int)
        and not isinstance(independent_actionable_count, bool)
        and independent_actionable_count > 0
    )
    throughput_actionable = (
        independently_actionable
        if cycle_mode == "release"
        else bool(actionable_nonterminal_cases)
    )

    if throughput_actionable and qualification["fail_on_systemic_research_blockers"]:
        failures.extend(
            f"shadow_qualification_systemic_research_blocker:{identity}:{code}"
            for identity, code in systemic_research_blockers
        )
    if throughput_actionable and qualification["require_nonempty_throughput"]:
        required_research = qualification["minimum_evidence_sufficient_research_proofs"]
        if len(model_produced_ready_research_proofs) < required_research:
            failures.append(
                "shadow_qualification_evidence_sufficient_research_throughput_"
                "below_minimum:"
                f"observed={len(model_produced_ready_research_proofs)}:"
                f"required={required_research}"
            )
        required_tickets = qualification["minimum_authoritative_ready_tickets"]
        if len(authoritative_ready_tickets) < required_tickets:
            failures.append(
                "shadow_qualification_authoritative_ready_ticket_throughput_"
                "below_minimum:"
                f"observed={len(authoritative_ready_tickets)}:required={required_tickets}"
            )

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
    independent_atoms = [atom for atom in atoms if atom_is_independent_problem_evidence(atom)]
    source_atom_projection = _source_atom_corpus_projection(independent_atoms)
    source_atom_ids = {
        atom_id
        for atom in independent_atoms
        for atom_id in [_text(atom.get("atom_id"))]
        if atom_id is not None
    }
    case_graph = _case_graph_projection(
        case_registry,
        source_atom_ids=source_atom_ids,
    )
    return {
        "schema_version": 4,
        "cycle_mode": cycle_mode,
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
            "productive_nonempty_cycle_contract": True,
            "independent_qualification_contract": cycle_mode == "release",
            "operational_internal_contract": cycle_mode == "operational",
            "systemic_research_blocker_contract": True,
        },
        "atom_corpus_sha256": _canonical_hash(atom_projection),
        "source_atom_corpus_sha256": _canonical_hash(source_atom_projection),
        "case_graph_sha256": _canonical_hash(case_graph),
        "ticket_set_sha256": _canonical_hash(
            _ticket_projection(backlog, source_atom_ids=source_atom_ids)
        ),
        "research_proof_basis_sha256": _canonical_hash(research_proof_basis),
        "qualification_basis_sha256": independent_qualification["basis_sha256"],
        "qualification_stability_sha256": independent_qualification["stability_sha256"],
        "qualification": independent_qualification,
        "counts": {
            "atoms": len(atoms),
            "cases": len(case_graph),
            "research_proofs": len(_items(stage3)),
            "change_plans": len(_items(stage6)),
            "tickets": len(tickets),
            "ready_tickets": sum(
                1 for ticket in tickets if _text(ticket.get("stage")) == "ready_for_ticket"
            ),
            "evidence_sufficient_research_proofs": sum(
                1
                for dossier in _items(stage3)
                if _text(dossier.get("research_status")) == "evidence_sufficient"
            ),
            "retained_ready_research_proofs": len(ready_research_proofs),
            "model_produced_evidence_sufficient_research_proofs": len(
                model_produced_ready_research_proofs
            ),
            "honest_case_specific_blocked_research": len(honest_case_specific_blocked_research),
            "systemic_research_blockers": len(systemic_research_blockers),
            "code_grounded_plans": len(code_grounded_plan_revisions),
            "authoritative_ready_tickets": len(authoritative_ready_tickets),
            "required_evidence_sufficient_research_proofs": qualification[
                "minimum_evidence_sufficient_research_proofs"
            ],
            "required_authoritative_ready_tickets": qualification[
                "minimum_authoritative_ready_tickets"
            ],
            "nonempty_throughput_enforced": int(qualification["require_nonempty_throughput"]),
            "systemic_research_blockers_enforced": int(
                qualification["fail_on_systemic_research_blockers"]
            ),
            "qualifying_observed_atoms": len(meaningful_observations),
            "actionable_nonterminal_cases": len(actionable_nonterminal_cases),
            "operational_actionable_cases": (
                len(actionable_nonterminal_cases) if cycle_mode == "operational" else 0
            ),
            "independent_actionable_cases": (
                independent_actionable_count
                if isinstance(independent_actionable_count, int)
                and not isinstance(independent_actionable_count, bool)
                else 0
            ),
            "independent_qualification_unknown": int(
                not isinstance(independent_actionable_count, int)
                or isinstance(independent_actionable_count, bool)
            ),
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
        "cycle_mode": report.get("cycle_mode"),
        "passed": report.get("passed"),
        "failures": report.get("failures"),
        "checks": report.get("checks"),
        **{field: report.get(field) for field in _INVARIANT_HASH_FIELDS},
        "export_projection_sha256": report.get("export_projection_sha256"),
        "export_inputs_sha256": report.get("export_inputs_sha256"),
        "stability_inputs_sha256": report.get("stability_inputs_sha256"),
        "qualification": report.get("qualification"),
        "counts": report.get("counts"),
    }


def _qualification_report_errors(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["shadow_independent_qualification_report_invalid"]
    errors: list[str] = []
    if value.get("schema_version") != 1:
        errors.append("shadow_independent_qualification_schema_invalid")
    limitations = value.get("limitations")
    if not isinstance(limitations, list) or any(_text(item) is None for item in limitations):
        errors.append("shadow_independent_qualification_limitations_invalid")
    policy = value.get("policy")
    policy_map = policy if isinstance(policy, Mapping) else {}
    if (
        not isinstance(policy, Mapping)
        or not isinstance(policy.get("positive_throughput_required"), bool)
        or isinstance(policy.get("minimum_good_ticket_count"), bool)
        or not isinstance(policy.get("minimum_good_ticket_count"), int)
        or policy.get("minimum_good_ticket_count", 0) < 1
        or isinstance(policy.get("minimum_good_to_bad_ratio"), bool)
        or not isinstance(policy.get("minimum_good_to_bad_ratio"), (int, float))
        or float(policy.get("minimum_good_to_bad_ratio", 0)) <= 1.0
        or isinstance(policy.get("minimum_recovered_to_missed_ratio"), bool)
        or not isinstance(policy.get("minimum_recovered_to_missed_ratio"), (int, float))
        or float(policy.get("minimum_recovered_to_missed_ratio", 0)) <= 1.0
        or policy.get("maximum_critical_bad_tickets") != 0
        or not isinstance(policy.get("require_zero_unknown_authoritative_tickets"), bool)
    ):
        errors.append("shadow_independent_qualification_policy_invalid")
    if value.get("status") not in {"verified", "failed", "missing", "invalid"}:
        errors.append("shadow_independent_qualification_status_invalid")
    qualification_class = value.get("qualification_class")
    if qualification_class not in {
        "positive_throughput",
        "verified_exhaustion",
        "unqualified",
    }:
        errors.append("shadow_independent_qualification_class_invalid")
    lifecycle_fields = (
        "clean_first_pass",
        "correction_required",
        "independent_release_evidence",
        "final_output_independently_adjudicated",
        "useful_output_verified",
    )
    present_lifecycle_fields = [field for field in lifecycle_fields if field in value]
    if present_lifecycle_fields and (
        len(present_lifecycle_fields) != len(lifecycle_fields)
        or any(not isinstance(value.get(field), bool) for field in lifecycle_fields)
    ):
        errors.append("shadow_independent_qualification_lifecycle_invalid")
    correction_metrics = value.get("correction_metrics")
    if present_lifecycle_fields and (
        not isinstance(correction_metrics, Mapping)
        or any(
            not isinstance(key, str)
            or not key
            or isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
            for key, item in (
                correction_metrics.items()
                if isinstance(correction_metrics, Mapping)
                else []
            )
        )
    ):
        errors.append("shadow_independent_qualification_correction_metrics_invalid")
    if present_lifecycle_fields and (
        value.get("clean_first_pass") is True
        and value.get("correction_required") is True
    ):
        errors.append("shadow_independent_qualification_first_pass_contradiction")
    if present_lifecycle_fields and value.get("useful_output_verified") is True and (
        value.get("status") != "verified"
        or qualification_class not in {"positive_throughput", "verified_exhaustion"}
    ):
        errors.append("shadow_independent_qualification_useful_output_mismatch")
    if not _valid_sha256(value.get("basis_sha256")):
        errors.append("shadow_independent_qualification_basis_invalid")
    if not _valid_sha256(value.get("stability_sha256")):
        errors.append("shadow_independent_qualification_stability_invalid")
    failures = value.get("failures")
    if not isinstance(failures, list) or any(_text(item) is None for item in failures):
        errors.append("shadow_independent_qualification_failures_invalid")
    counts = value.get("counts")
    if not isinstance(counts, Mapping) or any(
        not isinstance(key, str)
        or not key
        or (item is not None and (isinstance(item, bool) or not isinstance(item, int) or item < 0))
        for key, item in (counts.items() if isinstance(counts, Mapping) else [])
    ):
        errors.append("shadow_independent_qualification_counts_invalid")
    elif qualification_class == "positive_throughput" and not (
        value.get("status") == "verified"
        and policy_map.get("positive_throughput_required") is True
        and counts.get("actionable_cases", 0) > 0
        and counts.get("positive_qualifying_corpus") == 1
        and counts.get("exhausted_corpus") == 0
    ):
        errors.append("shadow_independent_qualification_class_mismatch")
    elif qualification_class == "verified_exhaustion" and not (
        value.get("status") == "verified"
        and counts.get("actionable_cases") == 0
        and counts.get("accepted_end_to_end_tickets") == 0
        and counts.get("exhausted_corpus") == 1
        and counts.get("positive_qualifying_corpus") == 0
    ):
        errors.append("shadow_independent_qualification_class_mismatch")
    elif (
        qualification_class == "unqualified"
        and isinstance(counts, Mapping)
        and (
            counts.get("positive_qualifying_corpus") == 1
            or counts.get("exhausted_corpus") == 1
            and value.get("status") == "verified"
        )
    ):
        errors.append("shadow_independent_qualification_class_mismatch")
    rates = value.get("rates")
    if not isinstance(rates, Mapping):
        errors.append("shadow_independent_qualification_rates_invalid")
    else:
        for name, rate in rates.items():
            if not isinstance(name, str) or not name or not isinstance(rate, Mapping):
                errors.append("shadow_independent_qualification_rates_invalid")
                break
            numerator = rate.get("numerator")
            denominator = rate.get("denominator")
            observed = rate.get("value")
            if any(
                item is not None
                and (isinstance(item, bool) or not isinstance(item, int) or item < 0)
                for item in (numerator, denominator)
            ):
                errors.append(f"shadow_independent_qualification_rate_invalid:{name}")
                continue
            expected = (
                None if numerator is None or denominator in {None, 0} else numerator / denominator
            )
            if observed != expected:
                errors.append(f"shadow_independent_qualification_rate_mismatch:{name}")
    ratio = value.get("good_to_bad_ratio")
    if not isinstance(ratio, Mapping) or ratio.get("status") not in {
        "finite",
        "infinite",
        "undefined",
        "unknown",
    }:
        errors.append("shadow_independent_qualification_ratio_invalid")
    elif isinstance(ratio.get("good"), int) and isinstance(ratio.get("bad"), int):
        good = ratio["good"]
        bad = ratio["bad"]
        expected_status = (
            "infinite" if bad == 0 and good > 0 else "undefined" if bad == 0 else "finite"
        )
        expected_value = None if bad == 0 else good / bad
        if ratio.get("status") != expected_status or ratio.get("value") != expected_value:
            errors.append("shadow_independent_qualification_ratio_mismatch")
    by_kind = value.get("by_kind")
    required_kinds = {
        "problem",
        "relation",
        "priority",
        "research",
        "option",
        "selection",
        "plan",
        "ticket",
    }
    if not isinstance(by_kind, Mapping) or set(by_kind) != required_kinds:
        errors.append("shadow_independent_qualification_by_kind_invalid")
    else:
        for kind, metrics in by_kind.items():
            if not isinstance(metrics, Mapping):
                errors.append(f"shadow_independent_qualification_kind_invalid:{kind}")
                continue
            kind_counts = metrics.get("counts")
            if not isinstance(kind_counts, Mapping) or any(
                item is not None
                and (isinstance(item, bool) or not isinstance(item, int) or item < 0)
                for item in (kind_counts.values() if isinstance(kind_counts, Mapping) else [])
            ):
                errors.append(f"shadow_independent_qualification_kind_counts_invalid:{kind}")
            kind_rates = metrics.get("rates")
            if not isinstance(kind_rates, Mapping):
                errors.append(f"shadow_independent_qualification_kind_rates_invalid:{kind}")
            else:
                for rate_name, rate in kind_rates.items():
                    if not isinstance(rate, Mapping):
                        errors.append(
                            f"shadow_independent_qualification_kind_rate_invalid:{kind}:{rate_name}"
                        )
                        continue
                    numerator = rate.get("numerator")
                    denominator = rate.get("denominator")
                    if any(
                        item is not None
                        and (isinstance(item, bool) or not isinstance(item, int) or item < 0)
                        for item in (numerator, denominator)
                    ):
                        errors.append(
                            f"shadow_independent_qualification_kind_rate_invalid:{kind}:{rate_name}"
                        )
                        continue
                    expected = (
                        None
                        if numerator is None or denominator in {None, 0}
                        else numerator / denominator
                    )
                    if rate.get("value") != expected:
                        errors.append(
                            "shadow_independent_qualification_kind_rate_mismatch:"
                            f"{kind}:{rate_name}"
                        )
            kind_ratio = metrics.get("good_to_bad_ratio")
            if not isinstance(kind_ratio, Mapping) or kind_ratio.get("status") not in {
                "finite",
                "infinite",
                "undefined",
                "unknown",
            }:
                errors.append(f"shadow_independent_qualification_kind_ratio_invalid:{kind}")
        if value.get("end_to_end") != by_kind.get("ticket"):
            errors.append("shadow_independent_qualification_end_to_end_mismatch")
        if isinstance(counts, Mapping):
            accepted_values = [
                metrics.get("counts", {}).get("accepted", 0)
                for metrics in by_kind.values()
                if isinstance(metrics, Mapping) and isinstance(metrics.get("counts"), Mapping)
            ]
            accepted_total = (
                sum(accepted_values)
                if all(
                    isinstance(item, int) and not isinstance(item, bool) for item in accepted_values
                )
                else None
            )
            if accepted_total is not None and counts.get("accepted_outputs") != accepted_total:
                errors.append("shadow_independent_qualification_accepted_total_mismatch")
            ticket_metrics = by_kind.get("ticket")
            ticket_counts = (
                ticket_metrics.get("counts")
                if isinstance(ticket_metrics, Mapping)
                and isinstance(ticket_metrics.get("counts"), Mapping)
                else {}
            )
            if counts.get("accepted_end_to_end_tickets") != ticket_counts.get("accepted"):
                errors.append("shadow_independent_qualification_ticket_total_mismatch")
            for overall_field, kind_field in (
                ("accepted_good", "good"),
                ("accepted_bad", "bad"),
                ("accepted_unknown", "unknown"),
                ("accepted_critical_bad", "critical_bad"),
                ("accepted_noncritical_bad", "noncritical_bad"),
                ("repaired", "repaired"),
                ("repair_unknown", "repair_unknown"),
            ):
                values = [
                    metrics.get("counts", {}).get(kind_field)
                    for metrics in by_kind.values()
                    if isinstance(metrics, Mapping) and isinstance(metrics.get("counts"), Mapping)
                ]
                expected = (
                    None
                    if any(item is None for item in values)
                    else sum(values)
                    if all(isinstance(item, int) and not isinstance(item, bool) for item in values)
                    else counts.get(overall_field)
                )
                if counts.get(overall_field) != expected:
                    errors.append(
                        f"shadow_independent_qualification_kind_sum_mismatch:{overall_field}"
                    )
    unknowns = value.get("unknowns")
    if not isinstance(unknowns, list) or any(_text(item) is None for item in unknowns):
        errors.append("shadow_independent_qualification_unknowns_invalid")
    routes_raw = value.get("correction_routes")
    routes = (
        [item for item in routes_raw if isinstance(item, Mapping)]
        if isinstance(routes_raw, list)
        else []
    )
    if not isinstance(routes_raw, list) or len(routes) != len(routes_raw):
        errors.append("shadow_independent_qualification_correction_routes_invalid")
    else:
        terminal_risks_raw = value.get("terminal_residual_risks", [])
        terminal_risks = (
            [item for item in terminal_risks_raw if isinstance(item, Mapping)]
            if isinstance(terminal_risks_raw, list)
            else []
        )
        if (
            not isinstance(terminal_risks_raw, list)
            or len(terminal_risks) != len(terminal_risks_raw)
        ):
            errors.append(
                "shadow_independent_qualification_terminal_residual_risks_invalid"
            )
        expected_routing_status = (
            "terminal_residual_risk"
            if routes
            and all(route.get("route_status") == "uncorrectable" for route in routes)
            else "pending_with_terminal_residual_risk"
            if terminal_risks
            else "pending_orchestration"
            if routes
            else "not_evaluated"
            if value.get("status") in {"missing", "invalid"}
            else "not_required"
        )
        if value.get("correction_routing_status") != expected_routing_status:
            errors.append("shadow_independent_qualification_correction_routing_status_invalid")
        for index, route in enumerate(routes):
            provenance = route.get("author_provenance")
            session_id = _text(route.get("agent_session_id"))
            if (
                qualification_correction_route_errors(route)
                or route.get("quality") not in {"bad", "unknown"}
                or route.get("consumption_status") != "pending_orchestration"
                or route.get("consumption_receipt") is not None
            ):
                errors.append(f"shadow_independent_qualification_correction_route_invalid:{index}")
                continue
            if route.get("route_status") == "same_author_resume" and not (
                session_id is not None
                and isinstance(provenance, Mapping)
                and provenance.get("exact_session_continuation") is True
                and provenance.get("workspace_continuity_verified") is True
                and _text(provenance.get("agent_session_id")) == session_id
                and _text(provenance.get("workspace_dir"))
                == _text(route.get("workspace_dir"))
            ):
                errors.append(
                    f"shadow_independent_qualification_correction_route_author_invalid:{index}"
                )
        if isinstance(counts, Mapping):
            route_count = counts.get("correction_routes")
            same_author_count = counts.get("same_author_correction_routes")
            unrouteable_count = counts.get("unrouteable_corrections")
            terminal_risk_count = counts.get(
                "source_correction_terminal_residual_risks"
            )
            if route_count is not None and route_count != len(routes):
                errors.append("shadow_independent_qualification_correction_route_count_mismatch")
            if same_author_count is not None and same_author_count != sum(
                route.get("route_status") == "same_author_resume" for route in routes
            ):
                errors.append("shadow_independent_qualification_same_author_route_count_mismatch")
            if unrouteable_count is not None and unrouteable_count != sum(
                route.get("route_status") == "author_provenance_unavailable" for route in routes
            ):
                errors.append("shadow_independent_qualification_unrouteable_route_count_mismatch")
            if (
                terminal_risk_count is not None
                and terminal_risk_count != len(terminal_risks)
            ):
                errors.append(
                    "shadow_independent_qualification_terminal_residual_risk_count_mismatch"
                )
    return errors


def _invariant_report_errors(report: Any) -> list[str]:
    if not isinstance(report, dict):
        return ["shadow_invariant_report_invalid"]
    errors: list[str] = []
    if report.get("schema_version") != 4:
        errors.append("shadow_invariant_report_schema_invalid")
    if report.get("cycle_mode") not in {"release", "operational"}:
        errors.append("shadow_invariant_cycle_mode_invalid")
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
    stability_inputs = report.get("stability_inputs_sha256")
    if stability_inputs is not None and not _valid_sha256(stability_inputs):
        errors.append("shadow_invariant_hash_invalid:stability_inputs_sha256")
    qualification_report = report.get("qualification")
    errors.extend(_qualification_report_errors(qualification_report))
    if isinstance(qualification_report, Mapping):
        if report.get("qualification_basis_sha256") != qualification_report.get("basis_sha256"):
            errors.append("shadow_independent_qualification_basis_mismatch")
        if report.get("qualification_stability_sha256") != qualification_report.get(
            "stability_sha256"
        ):
            errors.append("shadow_independent_qualification_stability_mismatch")
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


def write_pending_shadow_run(
    *,
    pending_path: Path,
    backlog_path: Path,
    artifact_paths: Mapping[str, Path | None],
    qualification_manifest_sha256_expected: str | None,
    output_adjudication_sha256_pre_run: str | None,
    generated_at: str,
) -> dict[str, Any]:
    """Seal one materialized model run for later independent adjudication.

    Output adjudication is intentionally excluded from the artifact projection: it
    must be created or replaced after this receipt. Every model-produced stage input,
    the rendered backlog, configs, and sealed qualification manifest remain bound.
    """

    if _text(generated_at) is None:
        raise ValueError("pending_shadow_run_generated_at_invalid")
    if qualification_manifest_sha256_expected is not None and not _valid_sha256(
        qualification_manifest_sha256_expected
    ):
        raise ValueError("pending_shadow_run_manifest_anchor_invalid")
    if output_adjudication_sha256_pre_run is not None and not _valid_sha256(
        output_adjudication_sha256_pre_run
    ):
        raise ValueError("pending_shadow_run_output_pre_hash_invalid")
    bound_paths = {
        name: path
        for name, path in artifact_paths.items()
        if name
        not in {
            "qualification.output_adjudication",
            "qualification.pending_run_receipt",
            "qualification.repair_bundle_manifest",
            "qualification.repaired_child_contract",
        }
    }
    payload: dict[str, Any] = {
        "schema_version": _PENDING_SHADOW_RUN_SCHEMA_VERSION,
        "contract_kind": "pending_shadow_run",
        "generated_at": generated_at,
        "backlog_path": str(backlog_path.resolve()),
        "backlog_sha256": _file_sha256(backlog_path),
        "backlog_content_sha256": _backlog_content_sha256(backlog_path),
        "qualification_manifest_sha256_expected": (qualification_manifest_sha256_expected),
        "output_adjudication_sha256_pre_run": (output_adjudication_sha256_pre_run),
        "artifact_receipts": _artifact_source_receipts(bound_paths),
    }
    payload["content_sha256"] = _canonical_hash(payload)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return payload


def validate_pending_shadow_run(
    *,
    pending_path: Path,
    backlog_path: Path,
    artifact_paths: Mapping[str, Path | None],
    recorded_backlog_path: Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate that phase-two scoring uses the exact phase-one materialization."""

    if not pending_path.is_file():
        return None, ["pending_shadow_run_missing"]
    try:
        raw = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"pending_shadow_run_unreadable:{type(exc).__name__}"]
    if not isinstance(raw, dict):
        return None, ["pending_shadow_run_invalid"]
    errors: list[str] = []
    if raw.get("schema_version") != _PENDING_SHADOW_RUN_SCHEMA_VERSION:
        errors.append("pending_shadow_run_schema_invalid")
    if raw.get("contract_kind") != "pending_shadow_run":
        errors.append("pending_shadow_run_kind_invalid")
    content_sha256 = raw.get("content_sha256")
    projected = {key: value for key, value in raw.items() if key != "content_sha256"}
    if not _valid_sha256(content_sha256) or content_sha256 != _canonical_hash(projected):
        errors.append("pending_shadow_run_content_hash_invalid")
    expected_recorded_path = (recorded_backlog_path or backlog_path).resolve()
    if raw.get("backlog_path") != str(expected_recorded_path):
        errors.append("pending_shadow_run_backlog_path_mismatch")
    try:
        if raw.get("backlog_sha256") != _file_sha256(backlog_path):
            errors.append("pending_shadow_run_backlog_changed")
        if raw.get("backlog_content_sha256") != _backlog_content_sha256(backlog_path):
            errors.append("pending_shadow_run_backlog_content_changed")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("pending_shadow_run_backlog_unavailable")
    bound_paths = {
        name: path
        for name, path in artifact_paths.items()
        if name
        not in {
            "qualification.output_adjudication",
            "qualification.pending_run_receipt",
            "qualification.repair_bundle_manifest",
            "qualification.repaired_child_contract",
        }
    }
    try:
        current_receipts = _artifact_source_receipts(bound_paths)
    except (OSError, ValueError) as exc:
        errors.append(f"pending_shadow_run_artifacts_invalid:{exc}")
    else:
        if raw.get("artifact_receipts") != current_receipts:
            errors.append("pending_shadow_run_materialized_artifacts_changed")
    return raw, list(dict.fromkeys(errors))


def _operational_pending_bound_paths(
    artifact_paths: Mapping[str, Path | None],
) -> dict[str, Path | None]:
    """Bind model outputs and generator inputs, leaving post-run UX review open."""

    return {
        name: path
        for name, path in artifact_paths.items()
        if name != "ux.review_json"
        and not name.startswith("qualification.")
        and name != "operational.pending_run_receipt"
    }


def write_pending_operational_shadow_run(
    *,
    pending_path: Path,
    backlog_path: Path,
    artifact_paths: Mapping[str, Path | None],
    generated_at: str,
) -> dict[str, Any]:
    """Seal one fresh model run before its independent UX artifact is produced.

    Routine refreshes do not consume held-out benchmark labels. The receipt still
    prevents an old backlog or changed stage output from being re-recorded as a new
    operational cycle after UX review.
    """

    if _text(generated_at) is None:
        raise ValueError("pending_operational_shadow_generated_at_invalid")
    payload: dict[str, Any] = {
        "schema_version": _PENDING_OPERATIONAL_SHADOW_RUN_SCHEMA_VERSION,
        "contract_kind": "pending_operational_shadow_run",
        "generated_at": generated_at,
        "backlog_path": str(backlog_path.resolve()),
        "backlog_sha256": _file_sha256(backlog_path),
        "backlog_content_sha256": _backlog_content_sha256(backlog_path),
        "artifact_receipts": _artifact_source_receipts(
            _operational_pending_bound_paths(artifact_paths)
        ),
    }
    payload["content_sha256"] = _canonical_hash(payload)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return payload


def validate_pending_operational_shadow_run(
    *,
    pending_path: Path,
    backlog_path: Path,
    artifact_paths: Mapping[str, Path | None],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Prove operational scoring consumes the exact fresh phase-one model run."""

    if not pending_path.is_file():
        return None, ["pending_operational_shadow_run_missing"]
    try:
        raw = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"pending_operational_shadow_run_unreadable:{type(exc).__name__}"]
    if not isinstance(raw, dict):
        return None, ["pending_operational_shadow_run_invalid"]
    errors: list[str] = []
    if raw.get("schema_version") != _PENDING_OPERATIONAL_SHADOW_RUN_SCHEMA_VERSION:
        errors.append("pending_operational_shadow_run_schema_invalid")
    if raw.get("contract_kind") != "pending_operational_shadow_run":
        errors.append("pending_operational_shadow_run_kind_invalid")
    content_sha256 = raw.get("content_sha256")
    projected = {key: value for key, value in raw.items() if key != "content_sha256"}
    if not _valid_sha256(content_sha256) or content_sha256 != _canonical_hash(projected):
        errors.append("pending_operational_shadow_run_content_hash_invalid")
    if raw.get("backlog_path") != str(backlog_path.resolve()):
        errors.append("pending_operational_shadow_run_backlog_path_mismatch")
    try:
        if raw.get("backlog_sha256") != _file_sha256(backlog_path):
            errors.append("pending_operational_shadow_run_backlog_changed")
        if raw.get("backlog_content_sha256") != _backlog_content_sha256(backlog_path):
            errors.append("pending_operational_shadow_run_backlog_content_changed")
    except (OSError, ValueError, json.JSONDecodeError):
        errors.append("pending_operational_shadow_run_backlog_unavailable")
    try:
        current_receipts = _artifact_source_receipts(
            _operational_pending_bound_paths(artifact_paths)
        )
    except (OSError, ValueError) as exc:
        errors.append(f"pending_operational_shadow_run_artifacts_invalid:{exc}")
    else:
        if raw.get("artifact_receipts") != current_receipts:
            errors.append("pending_operational_shadow_run_materialized_artifacts_changed")
    return raw, list(dict.fromkeys(errors))


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


def _stability_input_projection(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude held-out labels while retaining the sealed semantic input identity.

    Independent cycles must use distinct custody roots.  Their content-addressed
    inputs are therefore stable by receipt identity and bytes, not by the temporary
    path at which each cycle retained those bytes.
    """

    qualification_bundle_receipt = next(
        (
            receipt
            for receipt in receipts
            if receipt.get("name") == "qualification.input_bundle"
            and receipt.get("exists") is True
        ),
        None,
    )
    if qualification_bundle_receipt is not None:
        bundle_path_raw = qualification_bundle_receipt.get("snapshot_path") or (
            qualification_bundle_receipt.get("source_path")
        )
        bundle_path = (
            Path(bundle_path_raw)
            if isinstance(bundle_path_raw, str) and bundle_path_raw.strip()
            else None
        )
        try:
            bundle = (
                json.loads(bundle_path.read_text(encoding="utf-8"))
                if bundle_path is not None and bundle_path.is_file()
                else None
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            bundle = None
        pipeline = bundle.get("pipeline") if isinstance(bundle, Mapping) else None
        compatibility_sha256 = (
            pipeline.get("runtime_compatibility_sha256")
            if isinstance(pipeline, Mapping)
            else None
        )
        if _valid_sha256(compatibility_sha256):
            # Release qualification seals the full checkout for forensic fidelity,
            # while operational activation should be invalidated only by the actual
            # backlog runtime/config surface. Target implementation changes remain
            # bound to each research revision and do not force two unrelated release
            # cycles before every regeneration.
            return [
                {
                    "name": "pipeline.runtime_compatibility",
                    "sha256": compatibility_sha256,
                }
            ]

    projected = _artifact_source_projection(
        [
            receipt
            for receipt in receipts
            if receipt.get("name") not in _REQUIRED_SHADOW_ARTIFACTS
            and (
                receipt.get("name") == "qualification.input_bundle"
                or not str(receipt.get("name") or "").startswith("qualification.")
            )
            and not str(receipt.get("name") or "").startswith("operational.")
        ]
    )
    return [
        {key: value for key, value in receipt.items() if key != "source_path"}
        for receipt in projected
    ]


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
    if cycle.get("stability_inputs_sha256") != _canonical_hash(
        _stability_input_projection(valid_receipts)
    ):
        errors.append(f"shadow_cycle_stability_inputs_hash_mismatch:{cycle_id}")
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
    # knob. The latest full rendered projection and exact proof/plan hashes remain
    # bound to each cycle and to export, but nondeterministic independently-good
    # mechanisms do not have to be byte- or structure-identical across cycles.
    del require_exact_export_projection
    for field in (
        "source_atom_corpus_sha256",
        "qualification_stability_sha256",
    ):
        value = cycle.get(field)
        if not isinstance(value, str) or not value or value != reference.get(field):
            return False
    reference_stability_inputs = reference.get("stability_inputs_sha256")
    if (
        not _valid_sha256(reference_stability_inputs)
        or cycle.get("stability_inputs_sha256") != reference_stability_inputs
    ):
        return False
    return True


def _cycle_qualification_class(cycle: Mapping[str, Any]) -> str | None:
    qualification = cycle.get("qualification")
    if not isinstance(qualification, Mapping):
        return None
    return _text(qualification.get("qualification_class"))


def _cycle_mode(cycle: Mapping[str, Any]) -> str | None:
    mode = _text(cycle.get("cycle_mode"))
    return mode if mode in {"release", "operational"} else None


def _verified_exhaustion_cycle(cycle: Mapping[str, Any]) -> bool:
    """Return whether this exact empty corpus is safely exportable.

    Exhaustion is deliberately orthogonal to positive release qualification: it
    permits an operational refresh with no tickets, but never earns or extends the
    nondeterministic positive-throughput streak.
    """

    qualification = cycle.get("qualification")
    qualification_counts = (
        qualification.get("counts") if isinstance(qualification, Mapping) else None
    )
    cycle_counts = cycle.get("counts")
    return (
        _cycle_mode(cycle) == "release"
        and
        cycle.get("passed") is True
        and _cycle_qualification_class(cycle) == "verified_exhaustion"
        and isinstance(qualification, Mapping)
        and qualification.get("status") == "verified"
        and isinstance(qualification_counts, Mapping)
        and qualification_counts.get("exhausted_corpus") == 1
        and qualification_counts.get("accepted_end_to_end_tickets") == 0
        and isinstance(cycle_counts, Mapping)
        and cycle_counts.get("tickets") == 0
        and cycle_counts.get("ready_tickets") == 0
        and cycle_counts.get("authoritative_ready_tickets") == 0
    )


def _latest_release_streak(
    cycles: list[dict[str, Any]],
    *,
    required_consecutive_cycles: int,
    require_exact_export_projection: bool,
) -> list[dict[str, Any]]:
    """Return the latest explicit positive release streak, ignoring operations.

    Operational cycles may preserve an established release anchor but can never add
    to it. The latest explicit release failure invalidates older qualification.
    """

    release_cycles = [cycle for cycle in cycles if _cycle_mode(cycle) == "release"]
    if not release_cycles:
        return []
    reference = release_cycles[-1]
    if (
        reference.get("passed") is not True
        or _cycle_qualification_class(reference) != "positive_throughput"
        or not _cycle_matches_settings(
            reference,
            required_consecutive_cycles=required_consecutive_cycles,
            require_exact_export_projection=require_exact_export_projection,
        )
    ):
        return []
    streak: list[dict[str, Any]] = []
    for cycle in reversed(release_cycles):
        if (
            cycle.get("passed") is not True
            or _cycle_qualification_class(cycle) != "positive_throughput"
            or not _cycle_matches_settings(
                cycle,
                required_consecutive_cycles=required_consecutive_cycles,
                require_exact_export_projection=require_exact_export_projection,
            )
            or not _same_stability_basis(
                cycle,
                reference,
                require_exact_export_projection=require_exact_export_projection,
            )
        ):
            break
        streak.append(cycle)
    streak.reverse()
    return streak


def _operational_release_anchor(
    cycles: list[dict[str, Any]],
    *,
    required_consecutive_cycles: int,
    require_exact_export_projection: bool,
) -> list[dict[str, Any]]:
    if not cycles:
        return []
    latest = cycles[-1]
    if _cycle_mode(latest) != "operational" or latest.get("passed") is not True:
        return []
    streak = _latest_release_streak(
        cycles,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    if len(streak) < required_consecutive_cycles:
        return []
    anchor = streak[-1]
    if (
        latest.get("stability_inputs_sha256") != anchor.get("stability_inputs_sha256")
        or not _cycle_matches_settings(
            latest,
            required_consecutive_cycles=required_consecutive_cycles,
            require_exact_export_projection=require_exact_export_projection,
        )
    ):
        return []
    return streak[-required_consecutive_cycles:]


def _cycles_ready_for_export(
    cycles: list[dict[str, Any]],
    *,
    consecutive_positive_passes: int,
    required_consecutive_cycles: int,
) -> bool:
    if not cycles:
        return False
    latest = cycles[-1]
    if _verified_exhaustion_cycle(latest):
        return True
    if _cycle_mode(latest) == "release":
        return consecutive_positive_passes >= required_consecutive_cycles
    return bool(
        _operational_release_anchor(
            cycles,
            required_consecutive_cycles=required_consecutive_cycles,
            require_exact_export_projection=bool(
                latest.get("require_exact_export_projection")
            ),
        )
    )


def _consecutive_stable_passes(
    cycles: list[dict[str, Any]],
    *,
    required_consecutive_cycles: int,
    require_exact_export_projection: bool,
) -> int:
    streak = _latest_release_streak(
        cycles,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    if not cycles:
        return 0
    latest = cycles[-1]
    if _cycle_mode(latest) == "operational" and not _operational_release_anchor(
        cycles,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    ):
        return 0
    return len(streak)


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
    computed_ready = _cycles_ready_for_export(
        cycles,
        consecutive_positive_passes=consecutive,
        required_consecutive_cycles=required_cycles,
    )
    release_anchor = _operational_release_anchor(
        cycles,
        required_consecutive_cycles=required_cycles,
        require_exact_export_projection=exact,
    )
    if computed_ready and cycles:
        latest = cycles[-1]
        computed_activation_mode = (
            "verified_exhaustion"
            if _verified_exhaustion_cycle(latest)
            else (
                "operational_bound"
                if _cycle_mode(latest) == "operational"
                else "release_qualification"
            )
        )
    else:
        computed_activation_mode = None
    expected_anchor_ids = [cycle.get("cycle_id") for cycle in release_anchor]
    expected_anchor_stability = (
        release_anchor[-1].get("stability_inputs_sha256") if release_anchor else None
    )
    if recorded_consecutive != consecutive:
        errors.append("shadow_state_consecutive_count_mismatch")
    if state.get("ready_for_export") is not computed_ready:
        errors.append("shadow_state_readiness_mismatch")
    if state.get("activation_mode") != computed_activation_mode:
        errors.append("shadow_state_activation_mode_mismatch")
    if state.get("release_anchor_cycle_ids") != expected_anchor_ids:
        errors.append("shadow_state_release_anchor_ids_mismatch")
    if state.get("release_anchor_stability_inputs_sha256") != expected_anchor_stability:
        errors.append("shadow_state_release_anchor_stability_mismatch")
    validated_fields = (
        "validated_cycle_id",
        "validated_backlog_sha256",
        "validated_backlog_content_sha256",
        "validated_export_inputs_sha256",
        "validated_research_proof_basis_sha256",
        "validated_qualification_basis_sha256",
    )
    if computed_ready and cycles:
        latest = cycles[-1]
        expected = {
            "validated_cycle_id": latest.get("cycle_id"),
            "validated_backlog_sha256": latest.get("backlog_sha256"),
            "validated_backlog_content_sha256": latest.get("backlog_content_sha256"),
            "validated_export_inputs_sha256": latest.get("export_inputs_sha256"),
            "validated_research_proof_basis_sha256": latest.get("research_proof_basis_sha256"),
            "validated_qualification_basis_sha256": latest.get("qualification_basis_sha256"),
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
    stability_inputs_sha256 = _canonical_hash(_stability_input_projection(artifact_receipts))
    invariant_projection = _invariant_projection(
        {
            **invariant_report,
            "export_inputs_sha256": export_inputs_sha256,
            "stability_inputs_sha256": stability_inputs_sha256,
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
    recent_cycles = cycles[-max(10, required_consecutive_cycles) :]
    # Keep the latest explicit release attempts even across many routine cycles so
    # the anchor neither evaporates nor resurrects after a later release failure.
    latest_release_attempts = [
        item for item in cycles if _cycle_mode(item) == "release"
    ][-required_consecutive_cycles:]
    retained_ids = {
        str(item.get("cycle_id")) for item in [*recent_cycles, *latest_release_attempts]
    }
    cycles = [item for item in cycles if str(item.get("cycle_id")) in retained_ids]
    consecutive_stable_passes = _consecutive_stable_passes(
        cycles,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    stable = _cycles_ready_for_export(
        cycles,
        consecutive_positive_passes=consecutive_stable_passes,
        required_consecutive_cycles=required_consecutive_cycles,
    )
    release_anchor = _operational_release_anchor(
        cycles,
        required_consecutive_cycles=required_consecutive_cycles,
        require_exact_export_projection=require_exact_export_projection,
    )
    activation_mode = (
        (
            "verified_exhaustion"
            if _verified_exhaustion_cycle(cycle)
            else (
                "operational_bound"
                if _cycle_mode(cycle) == "operational"
                else "release_qualification"
            )
        )
        if stable
        else None
    )
    state = {
        "schema_version": _SHADOW_STATE_SCHEMA_VERSION,
        "backlog_path": str(backlog_path.resolve()),
        "ready_for_export": stable,
        "required_consecutive_cycles": required_consecutive_cycles,
        "require_exact_export_projection": require_exact_export_projection,
        "consecutive_stable_passes": consecutive_stable_passes,
        "activation_mode": activation_mode,
        "release_anchor_cycle_ids": [item["cycle_id"] for item in release_anchor],
        "release_anchor_stability_inputs_sha256": (
            release_anchor[-1]["stability_inputs_sha256"] if release_anchor else None
        ),
        "validated_cycle_id": cycle["cycle_id"] if stable else None,
        "validated_backlog_sha256": cycle["backlog_sha256"] if stable else None,
        "validated_backlog_content_sha256": (cycle["backlog_content_sha256"] if stable else None),
        "validated_export_inputs_sha256": (cycle["export_inputs_sha256"] if stable else None),
        "validated_research_proof_basis_sha256": (
            cycle["research_proof_basis_sha256"] if stable else None
        ),
        "validated_qualification_basis_sha256": (
            cycle["qualification_basis_sha256"] if stable else None
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
    computed_ready = _cycles_ready_for_export(
        cycles,
        consecutive_positive_passes=consecutive_stable_passes,
        required_consecutive_cycles=required_consecutive_cycles,
    )
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
    "operational_shadow_pending_run_path",
    "qualification_accepted_outputs",
    "record_shadow_cycle",
    "shadow_pending_run_path",
    "shadow_state_path",
    "validate_pending_shadow_run",
    "validate_pending_operational_shadow_run",
    "validate_shadow_export_state",
    "write_pending_shadow_run",
    "write_pending_operational_shadow_run",
]
