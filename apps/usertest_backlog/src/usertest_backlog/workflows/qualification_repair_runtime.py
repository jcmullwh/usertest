"""Production adapters for content-bound qualification correction.

The independent scorer supplies routes.  This module resumes the retained Stage 4-6
author conversations through their normal validators, then recomputes only downstream
model stages for affected cases.  It never writes tickets or queue state; callers
materialize the returned shadow artifacts in a separate namespace.

Stage 1 and Stage 3 use their evidence-capable exact-author adapters; Stage 2 and
Stages 4-6 use their normal stage correction loops. Every accepted repair is still
same-corpus feedback and must pass a fresh independent adjudication.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core import assemble_backlog_tickets, assess_research_readiness
from backlog_miner import BacklogProviderExternalWait
from backlog_miner.pipeline import PipelinePromptManifest
from backlog_miner.research_evidence import (
    ReplayExecutor,
    verify_persisted_research_evidence,
)
from backlog_miner.research_runner import (
    continue_research_dossier_from_independent_feedback,
)
from runner_core import RunnerConfig

from usertest_backlog.workflows.implementation_planning import (
    _run_implementation_planning_stage,
)
from usertest_backlog.workflows.prioritization import _run_problem_prioritization_stage
from usertest_backlog.workflows.problem_mining import (
    continue_problem_mining_from_independent_feedback,
    continue_problem_relation_review_from_independent_feedback,
)
from usertest_backlog.workflows.qualification_healing import (
    AuthorRevision,
    _compatible_route_groups,
    consume_qualification_corrections,
    correction_feedback_document,
)
from usertest_backlog.workflows.reproduction_research import _run_repro_research_stage
from usertest_backlog.workflows.solution_options import _run_solution_optioning_stage
from usertest_backlog.workflows.solution_selection import _run_solution_selection_stage


def _hash(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _items(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("items")
    return (
        [dict(item) for item in raw if isinstance(item, Mapping)]
        if isinstance(raw, list)
        else []
    )


def _payload_records(payload: Any) -> list[dict[str, Any]]:
    """Project a correction payload without falling back to an older stage frontier."""

    if isinstance(payload, Mapping):
        if isinstance(payload.get("items"), list):
            return _items(payload)
        return [dict(payload)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    return []


def _qualification_research_objective_best_frontier(
    document: Mapping[str, Any],
    *,
    problem_id: str,
) -> dict[str, Any] | None:
    """Recover the latest persisted runner-owned Stage-3 objective best for one problem."""

    meta_raw = document.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}

    def correction_frontier(value: Any) -> dict[str, Any] | None:
        correction = value if isinstance(value, Mapping) else {}
        frontier = correction.get("objective_best_frontier")
        return dict(frontier) if isinstance(frontier, Mapping) else None

    direct = correction_frontier(meta.get("qualification_research_correction"))
    if direct is not None:
        return direct
    history_raw = meta.get("qualification_repair_history")
    history = history_raw if isinstance(history_raw, list) else []
    for entry_raw in reversed(history):
        if not isinstance(entry_raw, Mapping):
            continue
        affected = {
            str(value)
            for value in entry_raw.get("affected_problem_ids", [])
            if isinstance(value, str)
        }
        if problem_id not in affected:
            continue
        replacement_raw = entry_raw.get("replacement_author_input_meta")
        replacement = replacement_raw if isinstance(replacement_raw, Mapping) else {}
        retained = correction_frontier(replacement.get("qualification_research_correction"))
        if retained is not None:
            return retained
    return None


def _stage3_external_wait(document: Mapping[str, Any]) -> dict[str, Any] | None:
    meta_raw = document.get("input_meta")
    meta = meta_raw if isinstance(meta_raw, Mapping) else {}
    checkpoint_raw = meta.get("external_wait")
    checkpoint = checkpoint_raw if isinstance(checkpoint_raw, Mapping) else {}
    wait_raw = checkpoint.get("external_wait")
    wait = wait_raw if isinstance(wait_raw, Mapping) else {}
    if (
        meta.get("stage_status") != "parked_external_wait"
        or checkpoint.get("status") != "parked_external_wait"
        or checkpoint.get("reason") != "codex_chatgpt_subscription_usage_limit"
        or checkpoint.get("route") != "chatgpt_subscription"
        or checkpoint.get("api_fallback_allowed") is not False
        or wait.get("code") != "codex_chatgpt_subscription_usage_limit"
        or wait.get("provider") != "codex"
        or wait.get("state") != "parked"
        or wait.get("route") != "chatgpt_subscription"
        or wait.get("api_fallback_allowed") is not False
    ):
        return None
    return dict(wait)


def _problem_id(record: Mapping[str, Any]) -> str | None:
    return _text(record.get("problem_id"))


def _case_id(record: Mapping[str, Any]) -> str | None:
    return _text(record.get("case_id"))


def _route_problem_id(route: Mapping[str, Any]) -> str | None:
    provenance = route.get("author_provenance")
    return _text(provenance.get("problem_id")) if isinstance(provenance, Mapping) else None


def _route_case_id(route: Mapping[str, Any]) -> str | None:
    provenance = route.get("author_provenance")
    return _text(provenance.get("case_id")) if isinstance(provenance, Mapping) else None


def _matches_route(record: Mapping[str, Any], route: Mapping[str, Any]) -> bool:
    pid = _route_problem_id(route)
    cid = _route_case_id(route)
    return bool(
        (pid is not None and _problem_id(record) == pid)
        or (cid is not None and _case_id(record) == cid)
    )


def _relation_affected_problem_ids(
    *,
    seed_problem_ids: set[str],
    before_records: Sequence[Mapping[str, Any]],
    after_records: Sequence[Mapping[str, Any]],
    before_decisions: Sequence[Mapping[str, Any]],
    after_decisions: Sequence[Mapping[str, Any]],
    before_registry: Mapping[str, Any],
    after_registry: Mapping[str, Any],
) -> set[str]:
    """Close a relation repair over the exact relation schema and case lineage."""

    relation_ids = set(seed_problem_ids)

    def record_ids(record: Mapping[str, Any]) -> set[str]:
        return {
            value
            for value in [
                _problem_id(record),
                *[
                    _text(item)
                    for item in (
                        record.get("case_member_problem_ids")
                        if isinstance(record.get("case_member_problem_ids"), list)
                        else []
                    )
                ],
            ]
            if value is not None
        }

    def record_atom_ids(record: Mapping[str, Any]) -> set[str]:
        return {
            atom_id
            for atom_id in (
                record.get("evidence_atom_ids")
                if isinstance(record.get("evidence_atom_ids"), list)
                else []
            )
            if isinstance(atom_id, str) and atom_id.strip()
        }

    records = [*before_records, *after_records]
    decisions = [*before_decisions, *after_decisions]
    changed = True
    while changed:
        changed = False
        for record in records:
            ids = record_ids(record)
            if ids.intersection(relation_ids) and not ids <= relation_ids:
                relation_ids.update(ids)
                changed = True
        for decision in decisions:
            decision_ids = {
                value
                for value in [
                    _text(decision.get("focus_id")),
                    _text(decision.get("alias_target_id")),
                    *[
                        _text(item)
                        for field in ("target_ids", "member_ids")
                        for item in (
                            decision.get(field)
                            if isinstance(decision.get(field), list)
                            else []
                        )
                    ],
                ]
                if value is not None
            }
            split_groups = decision.get("split_groups")
            split_atom_ids = {
                atom_id
                for group in (
                    split_groups if isinstance(split_groups, list) else []
                )
                if isinstance(group, Mapping)
                for atom_id in (
                    group.get("evidence_atom_ids")
                    if isinstance(group.get("evidence_atom_ids"), list)
                    else []
                )
                if isinstance(atom_id, str) and atom_id.strip()
            }
            if split_atom_ids:
                decision_ids.update(
                    problem_id
                    for record in records
                    if split_atom_ids.intersection(record_atom_ids(record))
                    for problem_id in record_ids(record)
                )
            if decision_ids.intersection(relation_ids) and not decision_ids <= relation_ids:
                relation_ids.update(decision_ids)
                changed = True

        for registry in (before_registry, after_registry):
            cases_raw = registry.get("cases") if isinstance(registry, Mapping) else None
            cases = cases_raw if isinstance(cases_raw, Mapping) else {}
            connected_case_ids: set[str] = set()
            for raw_case_id, raw_case in cases.items():
                if not isinstance(raw_case, Mapping):
                    continue
                case_id = _text(raw_case.get("case_id")) or str(raw_case_id)
                case_problem_ids = {
                    value
                    for value in [
                        _text(raw_case.get("canonical_problem_id")),
                        *[
                            _text(item)
                            for item in (
                                raw_case.get("problem_ids")
                                if isinstance(raw_case.get("problem_ids"), list)
                                else []
                            )
                        ],
                    ]
                    if value is not None
                }
                if case_problem_ids.intersection(relation_ids):
                    connected_case_ids.add(case_id)
            case_graph_changed = True
            while case_graph_changed:
                case_graph_changed = False
                for raw_case_id, raw_case in cases.items():
                    if not isinstance(raw_case, Mapping):
                        continue
                    case_id = _text(raw_case.get("case_id")) or str(raw_case_id)
                    linked_case_ids = {
                        value
                        for field in (
                            "alias_of",
                            "duplicate_of",
                            "split_from_case_id",
                        )
                        for value in [_text(raw_case.get(field))]
                        if value is not None
                    }
                    if (
                        case_id in connected_case_ids
                        or linked_case_ids.intersection(connected_case_ids)
                    ):
                        expanded = {case_id, *linked_case_ids}
                        if not expanded <= connected_case_ids:
                            connected_case_ids.update(expanded)
                            case_graph_changed = True
            for raw_case_id, raw_case in cases.items():
                if not isinstance(raw_case, Mapping):
                    continue
                case_id = _text(raw_case.get("case_id")) or str(raw_case_id)
                if case_id not in connected_case_ids:
                    continue
                case_problem_ids = {
                    value
                    for value in [
                        _text(raw_case.get("canonical_problem_id")),
                        *[
                            _text(item)
                            for item in (
                                raw_case.get("problem_ids")
                                if isinstance(raw_case.get("problem_ids"), list)
                                else []
                            )
                        ],
                    ]
                    if value is not None
                }
                if not case_problem_ids <= relation_ids:
                    relation_ids.update(case_problem_ids)
                    changed = True
    return relation_ids


_CAUSAL_STAGE_ORDER = {
    "problem_mining": 1,
    "problem_prioritization": 2,
    "repro_research": 3,
    "solution_optioning": 4,
    "solution_selection": 5,
    "implementation_planning": 6,
}
_STAGE1_COMPONENT_ORDER = {
    "problem_miner": 1,
    "coverage_review": 2,
    "relation_review": 3,
}


def _qualification_causal_graph(
    *,
    stage1: Mapping[str, Any],
    case_registry: Mapping[str, Any] | None,
) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}

    def connect(*nodes: str | None) -> None:
        retained = list(dict.fromkeys(node for node in nodes if node is not None))
        for node in retained:
            graph.setdefault(node, set()).update(other for other in retained if other != node)

    def prefixed(prefix: str, value: Any) -> str | None:
        normalized = _text(value)
        return f"{prefix}:{normalized}" if normalized is not None else None

    for record in _items(stage1):
        problem_nodes = [
            prefixed("problem", value)
            for value in [
                record.get("problem_id"),
                record.get("canonical_problem_id"),
                *(
                    record.get("case_member_problem_ids", [])
                    if isinstance(record.get("case_member_problem_ids"), list)
                    else []
                ),
                *(
                    record.get("split_parent_problem_ids", [])
                    if isinstance(record.get("split_parent_problem_ids"), list)
                    else []
                ),
            ]
        ]
        case_nodes = [
            prefixed("case", value)
            for value in [
                record.get("case_id"),
                record.get("split_from_case_id"),
                *(
                    record.get("absorbed_case_ids", [])
                    if isinstance(record.get("absorbed_case_ids"), list)
                    else []
                ),
            ]
        ]
        atom_nodes = [
            prefixed("atom", value)
            for value in (
                record.get("evidence_atom_ids", [])
                if isinstance(record.get("evidence_atom_ids"), list)
                else []
            )
        ]
        connect(*problem_nodes, *case_nodes, *atom_nodes)

    registry = case_registry if isinstance(case_registry, Mapping) else {}
    problem_map = registry.get("problem_id_to_case_id")
    if isinstance(problem_map, Mapping):
        for problem_id, case_id in problem_map.items():
            connect(prefixed("problem", problem_id), prefixed("case", case_id))
    for field in ("atom_id_to_case_id", "atom_id_to_case_ids"):
        atom_map = registry.get(field)
        if not isinstance(atom_map, Mapping):
            continue
        for atom_id, case_ids_raw in atom_map.items():
            case_ids = case_ids_raw if isinstance(case_ids_raw, list) else [case_ids_raw]
            connect(
                prefixed("atom", atom_id),
                *(prefixed("case", value) for value in case_ids),
            )
    cases_raw = registry.get("cases")
    cases = cases_raw if isinstance(cases_raw, Mapping) else {}
    for raw_case_id, raw_case in cases.items():
        if not isinstance(raw_case, Mapping):
            continue
        case_nodes = [
            prefixed("case", value)
            for value in [
                raw_case.get("case_id") or raw_case_id,
                raw_case.get("alias_of"),
                raw_case.get("duplicate_of"),
                raw_case.get("split_from_case_id"),
                *(
                    raw_case.get("absorbed_case_ids", [])
                    if isinstance(raw_case.get("absorbed_case_ids"), list)
                    else []
                ),
            ]
        ]
        problem_nodes = [
            prefixed("problem", value)
            for value in [
                raw_case.get("canonical_problem_id"),
                *(
                    raw_case.get("problem_ids", [])
                    if isinstance(raw_case.get("problem_ids"), list)
                    else []
                ),
            ]
        ]
        atom_nodes = [
            prefixed("atom", value)
            for value in (
                raw_case.get("evidence_atom_ids", [])
                if isinstance(raw_case.get("evidence_atom_ids"), list)
                else []
            )
        ]
        connect(*case_nodes, *problem_nodes, *atom_nodes)
    return graph


def _route_causal_tokens(
    route: Mapping[str, Any],
    *,
    graph: Mapping[str, set[str]],
) -> set[str]:
    target_raw = route.get("causal_target")
    target = target_raw if isinstance(target_raw, Mapping) else {}
    provenance_raw = route.get("author_provenance")
    provenance = provenance_raw if isinstance(provenance_raw, Mapping) else {}

    def values(field: str, fallback: Sequence[Any] = ()) -> list[str]:
        raw = target.get(field)
        candidates = raw if isinstance(raw, list) else list(fallback)
        return [
            value.strip()
            for value in candidates
            if isinstance(value, str) and value.strip()
        ]

    structural_seeds = {
        *(
            "problem:" + value
            for value in values(
                "problem_ids",
                [
                    provenance.get("problem_id"),
                    *(
                        provenance.get("relation_review_focus_ids", [])
                        if isinstance(provenance.get("relation_review_focus_ids"), list)
                        else []
                    ),
                ],
            )
        ),
        *(
            "case:" + value
            for value in values("case_ids", [provenance.get("case_id")])
        ),
        *(
            "atom:" + value
            for value in values(
                "evidence_atom_ids",
                (
                    provenance.get("evidence_atom_ids", [])
                    if isinstance(provenance.get("evidence_atom_ids"), list)
                    else []
                ),
            )
        ),
    }
    # Labels describe one adjudication group, not necessarily one causal component.
    # When exact problem/case/atom targets exist, use those targets so a held-out label
    # spanning disjoint miner assignments fans out to every exact author instead of the
    # first author blocking all siblings merely because they share the label name.
    seeds = structural_seeds or {
        "label:" + value
        for value in values(
            "actionable_label_ids",
            (
                route.get("actionable_label_ids", [])
                if isinstance(route.get("actionable_label_ids"), list)
                else []
            ),
        )
    }
    if not seeds:
        seeds.add("route:" + str(route.get("route_sha256") or ""))
    closed = set(seeds)
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        for adjacent in graph.get(node, set()):
            if adjacent not in closed:
                closed.add(adjacent)
                frontier.append(adjacent)
    return closed


def plan_qualification_repair_route_groups(
    routes: Sequence[Mapping[str, Any]],
    *,
    stage1: Mapping[str, Any],
    case_registry: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return deterministic exact-author groups and their causal scheduling status."""

    graph = _qualification_causal_graph(stage1=stage1, case_registry=case_registry)

    def rank(group: Sequence[Mapping[str, Any]]) -> tuple[int, int, str]:
        route = group[0]
        stage = _text(route.get("authoring_stage")) or ""
        provenance = route.get("author_provenance")
        adapter = (
            _text(provenance.get("stage1_correction_adapter"))
            if isinstance(provenance, Mapping)
            else None
        )
        return (
            _CAUSAL_STAGE_ORDER.get(stage, 99),
            _STAGE1_COMPONENT_ORDER.get(adapter or "", 99) if stage == "problem_mining" else 0,
            str(route.get("route_sha256") or ""),
        )

    claimed: list[tuple[set[str], str]] = []
    plan: list[dict[str, Any]] = []
    for group in sorted(_compatible_route_groups(routes), key=rank):
        route_hashes = [str(route["route_sha256"]) for route in group]
        tokens = {
            token
            for route in group
            for token in _route_causal_tokens(route, graph=graph)
        }
        blocker = next(
            (
                group_id
                for claimed_tokens, group_id in claimed
                if tokens.intersection(claimed_tokens)
            ),
            None,
        )
        component_cases = sorted(
            token.removeprefix("case:") for token in tokens if token.startswith("case:")
        )
        component_problems = sorted(
            token.removeprefix("problem:")
            for token in tokens
            if token.startswith("problem:")
        )
        component_basis = component_cases or component_problems or sorted(tokens)
        component_id = "causal_component:" + _hash(component_basis)
        group_id = "author_group:" + _hash(route_hashes)
        selected = blocker is None
        if selected:
            claimed.append((tokens, group_id))
        plan.append(
            {
                "group_id": group_id,
                "component_id": component_id,
                "route_sha256s": route_hashes,
                "causal_tokens": sorted(tokens),
                "causal_rank": list(rank(group)[:2]),
                "invocable": all(
                    route.get("route_status") == "same_author_resume" for route in group
                ),
                "disposition": (
                    "selected_causal_frontier"
                    if selected
                    else "retained_pending_causal_predecessor"
                ),
                "blocked_by_group_id": blocker,
            }
        )
    return plan


def _records_for_problem(
    records: Sequence[Mapping[str, Any]],
    *,
    problem_id: str,
) -> list[dict[str, Any]]:
    return [dict(record) for record in records if _problem_id(record) == problem_id]


def _merge_problem_items(
    original: Mapping[str, Any],
    replacement: Mapping[str, Any],
    *,
    problem_ids: set[str],
    repair_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    def item_problem_ids(item: Mapping[str, Any]) -> set[str]:
        return {
            value
            for value in [
                _problem_id(item),
                *[
                    _text(member)
                    for member in (
                        item.get("case_member_problem_ids")
                        if isinstance(item.get("case_member_problem_ids"), list)
                        else []
                    )
                ],
            ]
            if value is not None
        }

    old_items = [
        item for item in _items(original) if not item_problem_ids(item).intersection(problem_ids)
    ]
    new_items = [
        item
        for item in _items(replacement)
        if item_problem_ids(item).intersection(problem_ids)
    ]
    merged = dict(replacement)
    merged["items"] = [*old_items, *new_items]
    original_meta_raw = original.get("input_meta")
    original_meta = (
        dict(original_meta_raw) if isinstance(original_meta_raw, Mapping) else {}
    )
    replacement_meta_raw = replacement.get("input_meta")
    replacement_meta = (
        dict(replacement_meta_raw)
        if isinstance(replacement_meta_raw, Mapping)
        else {}
    )
    # The top-level author metadata continues to describe every unchanged sibling.
    # Replacement metadata is scoped to the affected problems in repair history so a
    # later qualification cycle can recover either exact frontier without ambiguity.
    meta = dict(original_meta)
    prior_repair_raw = original_meta.get("qualification_repair")
    prior_repair = prior_repair_raw if isinstance(prior_repair_raw, Mapping) else {}
    cumulative_receipts = [
        dict(item)
        for item in prior_repair.get("route_consumption_receipts", [])
        if isinstance(item, Mapping)
    ]
    known_receipt_hashes = {
        _text(item.get("consumption_receipt_sha256")) for item in cumulative_receipts
    }
    cumulative_receipts.extend(
        dict(item)
        for item in repair_receipts
        if _text(item.get("consumption_receipt_sha256")) not in known_receipt_hashes
    )
    prior_history_raw = original_meta.get("qualification_repair_history")
    prior_history = (
        [dict(item) for item in prior_history_raw if isinstance(item, Mapping)]
        if isinstance(prior_history_raw, list)
        else []
    )
    replacement_author_meta = {
        key: value
        for key, value in replacement_meta.items()
        if key not in {"qualification_repair", "qualification_repair_history"}
    }
    repair_history_entry = {
        "affected_problem_ids": sorted(problem_ids),
        "replacement_stage_document_sha256": _hash(replacement),
        "replacement_author_input_meta": replacement_author_meta,
        "route_consumption_receipts": [dict(item) for item in repair_receipts],
    }
    meta["qualification_repair_history"] = [
        *prior_history,
        repair_history_entry,
    ]
    meta["qualification_repair"] = {
        "base_stage_document_sha256": _hash(original),
        "affected_problem_ids": sorted(problem_ids),
        "retained_unchanged_item_count": len(old_items),
        "replacement_item_count": len(new_items),
        "route_consumption_receipts": cumulative_receipts,
        "same_corpus_feedback_exposed": True,
        "release_qualification_eligible": False,
    }
    merged["input_meta"] = meta
    original_artifacts_raw = original.get("artifacts")
    replacement_artifacts_raw = replacement.get("artifacts")
    original_artifacts = (
        dict(original_artifacts_raw)
        if isinstance(original_artifacts_raw, Mapping)
        else {}
    )
    replacement_artifacts = (
        dict(replacement_artifacts_raw)
        if isinstance(replacement_artifacts_raw, Mapping)
        else {}
    )
    artifacts_by_problem_raw = original_artifacts.get(
        "qualification_repair_artifacts_by_problem"
    )
    artifacts_by_problem = (
        dict(artifacts_by_problem_raw)
        if isinstance(artifacts_by_problem_raw, Mapping)
        else {}
    )
    for problem_id in problem_ids:
        artifacts_by_problem[problem_id] = replacement_artifacts
    current_evidence_receipt = (
        replacement_artifacts.get("qualification_repair_current_evidence_receipt")
        or replacement_artifacts.get("problem_mining_evidence_receipt")
        or original_artifacts.get("qualification_repair_current_evidence_receipt")
        or original_artifacts.get("problem_mining_evidence_receipt")
    )
    merged["artifacts"] = {
        **original_artifacts,
        "qualification_repair_artifacts_by_problem": artifacts_by_problem,
        **(
            {
                "qualification_repair_current_evidence_receipt": (
                    current_evidence_receipt
                )
            }
            if current_evidence_receipt is not None
            else {}
        ),
    }
    return merged


def _author_binding_from_stage_doc(
    document: Mapping[str, Any],
    *,
    stage: str,
    problem_id: str,
) -> tuple[str | None, str | None, list[str]]:
    meta = document.get("input_meta")
    meta_map = meta if isinstance(meta, Mapping) else {}
    if stage == "problem_prioritization":
        attempts_raw = meta_map.get("prioritizer_attempt_history")
        attempts = (
            [item for item in attempts_raw if isinstance(item, Mapping)]
            if isinstance(attempts_raw, list)
            else []
        )
        retained = next(
            (attempt for attempt in reversed(attempts) if attempt.get("status") == "verified"),
            attempts[-1] if attempts else None,
        )
        if not isinstance(retained, Mapping):
            return None, None, [
                f"qualification_repair_author_run_missing:{stage}:{problem_id}"
            ]
        session_id = _text(retained.get("agent_session_id"))
        workspace_dir = _text(retained.get("workspace_dir"))
        errors: list[str] = []
        if session_id is None:
            errors.append(
                f"qualification_repair_author_session_missing:{stage}:{problem_id}"
            )
        if workspace_dir is None:
            errors.append(
                f"qualification_repair_author_workspace_missing:{stage}:{problem_id}"
            )
        return session_id, workspace_dir, errors
    if stage == "solution_optioning":
        raw_runs = meta_map.get("optioning_correction_runs")
        roles = {None}
    elif stage == "solution_selection":
        raw_runs = meta_map.get("role_healing_runs")
        roles = {"selector"}
    else:
        raw_runs = meta_map.get("planning_correction_runs")
        roles = {"planner", None}
    runs = (
        [item for item in raw_runs if isinstance(item, Mapping)]
        if isinstance(raw_runs, list)
        else []
    )
    run = next(
        (
            item
            for item in reversed(runs)
            if _text(item.get("problem_id")) == problem_id
            and _text(item.get("role")) in roles
        ),
        None,
    )
    if not isinstance(run, Mapping):
        return None, None, [f"qualification_repair_author_run_missing:{stage}:{problem_id}"]
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
    session_id = _text(run.get("session_id")) or (
        _text(retained.get("agent_session_id")) if isinstance(retained, Mapping) else None
    )
    workspace_dir = (
        _text(retained.get("workspace_dir")) if isinstance(retained, Mapping) else None
    ) or _text(run.get("workspace_dir"))
    errors: list[str] = []
    if session_id is None:
        errors.append(f"qualification_repair_author_session_missing:{stage}:{problem_id}")
    if workspace_dir is None:
        errors.append(f"qualification_repair_author_workspace_missing:{stage}:{problem_id}")
    return session_id, workspace_dir, errors


def _recorded_attempt_cost(value: Any) -> float:
    """Recover model time from retained attempt receipts without double-counting."""

    attempts: dict[str, float] = {}

    def visit(candidate: Any) -> None:
        if isinstance(candidate, Mapping):
            elapsed_values = [
                candidate.get(field)
                for field in (
                    "elapsed_seconds",
                    "attempt_elapsed_seconds",
                    "attempt_wall_seconds",
                )
                if isinstance(candidate.get(field), (int, float))
                and not isinstance(candidate.get(field), bool)
            ]
            elapsed = max((float(item) for item in elapsed_values), default=None)
            identifying = any(
                key in candidate
                for key in (
                    "attempt_number",
                    "attempt_tag",
                    "prompt_sha256",
                    "failure_identity",
                )
            )
            if (
                identifying
                and elapsed is not None
            ):
                identity = _hash(
                    {
                        key: candidate.get(key)
                        for key in (
                            "attempt_number",
                            "attempt_tag",
                            "prompt_sha256",
                            "response_sha256",
                            "failure_identity",
                            "agent_session_id",
                        )
                    }
                )
                attempts[identity] = max(attempts.get(identity, 0.0), elapsed)
            for nested in candidate.values():
                visit(nested)
        elif isinstance(candidate, Sequence) and not isinstance(
            candidate, (str, bytes, bytearray)
        ):
            for nested in candidate:
                visit(nested)

    visit(value)
    return sum(max(0.0, elapsed) for elapsed in attempts.values())


def _stage_failure_errors(
    document: Mapping[str, Any],
    *,
    stage: str,
    problem_id: str,
) -> list[str]:
    items = _items(document)
    if items:
        return []
    meta = document.get("input_meta")
    meta_map = meta if isinstance(meta, Mapping) else {}
    outcome_key_and_statuses = {
        "solution_optioning": (
            "optioning_outcomes",
            "optioning_status",
            {"insufficient_evidence", "no_safe_option"},
        ),
        "solution_selection": (
            "selection_outcomes",
            "selection_status",
            {"insufficient_evidence", "no_safe_option", "reject"},
        ),
        "implementation_planning": (
            "planning_outcomes",
            "planning_status",
            {"research_required", "blocked", "no_safe_plan"},
        ),
    }
    terminal_contract = outcome_key_and_statuses.get(stage)
    if terminal_contract is not None:
        outcome_key, status_key, statuses = terminal_contract
        outcomes_raw = meta_map.get(outcome_key)
        outcomes = (
            [item for item in outcomes_raw if isinstance(item, Mapping)]
            if isinstance(outcomes_raw, list)
            else []
        )
        if any(
            _problem_id(outcome) == problem_id
            and _text(outcome.get(status_key)) in statuses
            for outcome in outcomes
        ):
            return []
    candidates = [
        value
        for key, value in meta_map.items()
        if key.endswith("_warnings") or key.endswith("_status") or key.endswith("_outcomes")
    ]
    diagnostics = [
        str(value)
        for candidate in candidates
        for value in (candidate if isinstance(candidate, list) else [candidate])
        if str(value).strip()
    ]
    return diagnostics or [f"qualification_repair_no_valid_output:{stage}"]


@dataclass
class QualificationRepairRuntimeResult:
    consumption: dict[str, Any]
    stage_documents: dict[str, dict[str, Any]]
    tickets: list[dict[str, Any]]
    affected_problem_ids: list[str]
    atoms: list[dict[str, Any]] | None = None
    case_registry: dict[str, Any] | None = None


def run_stage456_qualification_repairs(
    *,
    routes: Sequence[Mapping[str, Any]],
    source_pending_run_sha256: str,
    source_adjudication_sha256: str,
    repo_root: Path,
    atoms: list[dict[str, Any]],
    stage1: Mapping[str, Any],
    stage2: Mapping[str, Any],
    stage3: Mapping[str, Any],
    stage4: Mapping[str, Any],
    stage5: Mapping[str, Any],
    stage6: Mapping[str, Any],
    pipeline_manifest: PipelinePromptManifest,
    repair_artifacts_dir: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    breadth_profile: str,
    repo_input: str | None = None,
    research_ref: str | None = None,
    replay_timeout_seconds: float | None = None,
    replay_executor: ReplayExecutor | None = None,
    replay_executor_metadata: Mapping[str, Any] | None = None,
    target_slug: str | None = None,
    case_registry: Mapping[str, Any] | None = None,
    qualification_manifest: Mapping[str, Any] | None = None,
    resume_frontiers: Mapping[str, Mapping[str, Any]] | None = None,
) -> QualificationRepairRuntimeResult:
    """Resume Stage 4-6 authors and rebuild only affected downstream cases."""

    repair_artifacts_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "problem_mining": dict(stage1),
        "problem_prioritization": dict(stage2),
        "repro_research": dict(stage3),
        "solution_optioning": dict(stage4),
        "solution_selection": dict(stage5),
        "implementation_planning": dict(stage6),
    }
    current_case_registry = (
        dict(case_registry) if isinstance(case_registry, Mapping) else {}
    )
    candidate_docs: dict[str, dict[str, Any]] = {}
    candidate_atoms: dict[str, list[dict[str, Any]]] = {}
    candidate_case_registries: dict[str, dict[str, Any]] = {}
    candidate_problem_ids: dict[str, set[str]] = {}
    stage1_correction_attempts: dict[str, list[dict[str, Any]]] = {}
    stage4_guidance = pipeline_manifest.load_stage_guidance("solution_optioning")
    stage5_guidance = pipeline_manifest.load_stage_guidance("solution_selection")
    stage6_guidance = pipeline_manifest.load_stage_guidance("implementation_planning")
    stage1_guidance = pipeline_manifest.load_stage_guidance("problem_mining")
    stage2_guidance = pipeline_manifest.load_stage_guidance("problem_prioritization")

    problem_records = _items(stage1)
    priority_decisions = _items(stage2)
    research_dossiers = _items(stage3)
    solution_options = _items(stage4)
    selection_decisions = _items(stage5)

    def target_roots(problem_ids: set[str]) -> dict[str, Path]:
        roots: dict[str, Path] = {}
        for dossier in research_dossiers:
            pid = _problem_id(dossier)
            workspace = _text(dossier.get("repo_workspace"))
            if pid in problem_ids and workspace is not None:
                roots[str(pid)] = Path(workspace).expanduser().resolve()
        return roots

    def current_payload(route: Mapping[str, Any]) -> Any:
        stage = _text(route.get("authoring_stage"))
        if stage in documents:
            matching = [item for item in _items(documents[stage]) if _matches_route(item, route)]
            if matching:
                return matching[0] if len(matching) == 1 else matching
        # A ticket is planner-authored but lives outside Stage 6.  Use the retained plan
        # for the exact case as the current author frontier.
        matching_plans = [item for item in _items(stage6) if _matches_route(item, route)]
        return matching_plans[0] if len(matching_plans) == 1 else matching_plans

    def invoke_exact_author(**kwargs: Any) -> AuthorRevision:
        attempt_started = time.monotonic()
        route = kwargs["route"]
        feedback = kwargs["feedback"]
        stage = _text(route.get("authoring_stage"))
        pid = _route_problem_id(route)
        if stage not in {
            "problem_mining",
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
        } or (pid is None and stage != "problem_mining"):
            # Do not pretend a planning adapter can regenerate evidence receipts.  The
            # generic correction frontier retains this explicit unresolved state without
            # invoking a model or replacing authored work.
            return AuthorRevision(
                payload=kwargs["current_payload"],
                validation_errors=(f"qualification_repair_adapter_unavailable:{stage}",),
                valid_item_keys=(),
                agent_session_id=str(route.get("agent_session_id") or ""),
                workspace_dir=str(route.get("workspace_dir") or ""),
                cost_seconds=0.0,
            )

        def revision(
            *,
            payload: Any,
            validation_errors: Sequence[str],
            valid_item_keys: Sequence[str],
            agent_session_id: str,
            workspace_dir: str,
            receipt_evidence: Any = None,
        ) -> AuthorRevision:
            return AuthorRevision(
                payload=payload,
                validation_errors=tuple(validation_errors),
                valid_item_keys=tuple(valid_item_keys),
                agent_session_id=agent_session_id,
                workspace_dir=workspace_dir,
                cost_seconds=max(
                    max(0.0, time.monotonic() - attempt_started),
                    _recorded_attempt_cost(receipt_evidence),
                ),
            )
        correction = {
            "agent_session_id": route["agent_session_id"],
            "original_author_cost_seconds": (
                route.get("author_provenance", {}).get("original_author_cost_seconds")
                if isinstance(route.get("author_provenance"), Mapping)
                else None
            ),
            "feedback": feedback,
        }
        pid_set = {pid} if pid is not None else set()
        route_dir = repair_artifacts_dir / str(route["route_sha256"][:16])
        route_dir.mkdir(parents=True, exist_ok=True)
        roots = target_roots(pid_set)
        if stage == "problem_mining":
            route_key = str(route["route_sha256"])
            current_stage1 = documents["problem_mining"]
            labels_raw = (
                qualification_manifest.get("atom_labels")
                if isinstance(qualification_manifest, Mapping)
                else None
            )
            labels = (
                [item for item in labels_raw if isinstance(item, Mapping)]
                if isinstance(labels_raw, list)
                else []
            )
            route_label_ids = {
                str(item)
                for item in route.get("actionable_label_ids", [])
                if isinstance(item, str) and item.strip()
            }
            actionable_atom_ids = [
                str(atom_id)
                for label in labels
                if _text(label.get("label_id")) in route_label_ids
                for atom_id in (
                    label.get("atom_ids")
                    if isinstance(label.get("atom_ids"), list)
                    else []
                )
                if isinstance(atom_id, str) and atom_id.strip()
            ]
            provenance_raw = route.get("author_provenance")
            provenance = (
                provenance_raw if isinstance(provenance_raw, Mapping) else {}
            )
            provenance_evidence_ids = [
                value
                for value in provenance.get("evidence_atom_ids", [])
                if isinstance(value, str) and value.strip()
            ]
            grouped_evidence_ids = [
                value
                for value in feedback.get("evidence_atom_ids", [])
                if isinstance(value, str) and value.strip()
            ]
            causal_target_raw = route.get("causal_target")
            causal_target = (
                causal_target_raw
                if isinstance(causal_target_raw, Mapping)
                else {}
            )
            explicit_target_atom_ids = [
                value
                for value in causal_target.get("evidence_atom_ids", [])
                if isinstance(value, str) and value.strip()
            ]
            target_atom_ids = list(
                dict.fromkeys(
                    explicit_target_atom_ids
                    or [
                        *actionable_atom_ids,
                        *provenance_evidence_ids,
                        *grouped_evidence_ids,
                    ]
                )
            )
            registry = dict(current_case_registry)
            common_stage1_args = {
                "stage_doc": dict(current_stage1),
                "atoms": atoms,
                "feedback": feedback,
                "pipeline_manifest": pipeline_manifest,
                "stage_guidance_text": stage1_guidance,
                "artifacts_dir": route_dir,
                "out_json": route_dir / "problem_records.json",
                "out_md": route_dir / "problem_records.md",
                "case_registry_path": route_dir / "case_registry.json",
                "previous_case_registry": registry,
                "repo_root": repo_root,
                "agent": agent,
                "model": model,
                "cfg": cfg,
                "prior_correction_attempts": stage1_correction_attempts.get(
                    route_key, []
                ),
                "prior_correction_errors": (
                    list(kwargs["prior_assessment"].introduced_error_identities)
                    if kwargs.get("prior_assessment") is not None
                    else []
                ),
                "correction_attempt_number": int(kwargs.get("attempt_number") or 1),
            }
            if provenance.get("stage1_correction_adapter") == "relation_review":
                stage1_result = (
                    continue_problem_relation_review_from_independent_feedback(
                        **common_stage1_args,
                        author_provenance=provenance,
                    )
                )
            else:
                stage1_result = continue_problem_mining_from_independent_feedback(
                    **common_stage1_args,
                    actionable_atom_ids=target_atom_ids,
                    author_component=_text(
                        provenance.get("stage1_correction_adapter")
                    ),
                    author_provenance=provenance,
                )
            corrected_doc_raw = stage1_result.get("stage_doc")
            corrected_doc = (
                dict(corrected_doc_raw)
                if isinstance(corrected_doc_raw, Mapping)
                else dict(current_stage1)
            )
            candidate_docs[route_key] = corrected_doc
            attempt_record_raw = stage1_result.get("attempt_record")
            if isinstance(attempt_record_raw, Mapping):
                stage1_correction_attempts.setdefault(route_key, []).append(
                    dict(attempt_record_raw)
                )
            atoms_raw = stage1_result.get("atoms")
            candidate_atoms[route_key] = (
                [dict(item) for item in atoms_raw if isinstance(item, Mapping)]
                if isinstance(atoms_raw, list)
                else atoms
            )
            registry_raw = stage1_result.get("case_registry")
            candidate_case_registries[route_key] = (
                dict(registry_raw) if isinstance(registry_raw, Mapping) else registry
            )
            affected_records = [
                record
                for record in _items(corrected_doc)
                if set(
                    item
                    for item in record.get("evidence_atom_ids", [])
                    if isinstance(item, str)
                ).intersection(target_atom_ids)
            ]
            affected_ids = {
                problem_id
                for record in affected_records
                for problem_id in [_problem_id(record)]
                if problem_id is not None
            }
            route_problem_id = _route_problem_id(route)
            if route_problem_id is not None:
                affected_ids.add(route_problem_id)
            # Every Stage-1 correction can rerun relation review. Primary-miner and
            # coverage fixes therefore have the same merge/alias/split closure duty as
            # an explicitly targeted relation-review correction.
            before_meta_raw = current_stage1.get("input_meta")
            after_meta_raw = corrected_doc.get("input_meta")
            before_decisions_raw = (
                before_meta_raw.get("relation_review_decisions")
                if isinstance(before_meta_raw, Mapping)
                else None
            )
            after_decisions_raw = (
                after_meta_raw.get("relation_review_decisions")
                if isinstance(after_meta_raw, Mapping)
                else None
            )
            affected_ids = _relation_affected_problem_ids(
                seed_problem_ids={
                    *affected_ids,
                    *{
                        value
                        for value in provenance.get(
                            "relation_review_focus_ids", []
                        )
                        if isinstance(value, str) and value.strip()
                    },
                },
                before_records=_items(current_stage1),
                after_records=_items(corrected_doc),
                before_decisions=[
                    item
                    for item in (
                        before_decisions_raw
                        if isinstance(before_decisions_raw, list)
                        else []
                    )
                    if isinstance(item, Mapping)
                ],
                after_decisions=[
                    item
                    for item in (
                        after_decisions_raw
                        if isinstance(after_decisions_raw, list)
                        else []
                    )
                    if isinstance(item, Mapping)
                ],
                before_registry=registry,
                after_registry=candidate_case_registries.get(route_key, {}),
            )
            affected_ids.update(
                value
                for finding in feedback.get("findings", [])
                if isinstance(finding, Mapping)
                for value in [_text(finding.get("problem_id"))]
                if value is not None
            )
            candidate_problem_ids[route_key] = affected_ids
            errors = [
                str(error)
                for error in stage1_result.get("validation_errors", [])
                if str(error).strip()
            ]
            if stage1_result.get("status") != "corrected" and not errors:
                errors.append(
                    "qualification_stage1_correction_incomplete:"
                    + str(stage1_result.get("status"))
                )
            return revision(
                payload=corrected_doc,
                validation_errors=tuple(errors),
                valid_item_keys=tuple(
                    sorted(
                        {
                            *("problem:" + value for value in affected_ids),
                            *(
                                "atom:" + value
                                for value in target_atom_ids
                                if stage1_result.get("status") == "corrected"
                            ),
                        }
                    )
                ),
                agent_session_id=str(stage1_result.get("agent_session_id") or ""),
                workspace_dir=str(stage1_result.get("workspace_dir") or ""),
                receipt_evidence=stage1_result,
            )
        if stage == "problem_prioritization":
            document = _run_problem_prioritization_stage(
                atoms=atoms,
                problem_records=problem_records,
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=route_dir,
                out_json=route_dir / "prioritized_problems.json",
                out_md=route_dir / "prioritized_problems.md",
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=False,
                stage_guidance_text=stage2_guidance,
                external_correction={
                    **correction,
                    "current_payload": _items(stage2),
                },
            )
            candidate_docs[str(route["route_sha256"])] = document
            session_id, workspace_dir, binding_errors = _author_binding_from_stage_doc(
                document,
                stage=stage,
                problem_id=str(pid),
            )
            errors = [
                *_stage_failure_errors(
                    document,
                    stage=stage,
                    problem_id=str(pid),
                ),
                *binding_errors,
            ]
            all_items = _items(document)
            matching_items = _records_for_problem(all_items, problem_id=str(pid))
            return revision(
                payload=all_items,
                validation_errors=errors,
                valid_item_keys=(
                    tuple(
                        "priority_decision:" + value
                        for item in all_items
                        for value in [_problem_id(item)]
                        if value is not None
                    )
                    if matching_items and not errors
                    else ()
                ),
                agent_session_id=session_id or "",
                workspace_dir=workspace_dir or "",
                receipt_evidence=document,
            )
        if stage == "repro_research":
            # The generic correction controller owns the active frontier.  Reusing the
            # Stage-3 document captured before correction caused every outer turn to reopen
            # the original dossier, even after the same author had produced a complete one.
            source_dossiers = _records_for_problem(
                _payload_records(kwargs["current_payload"]),
                problem_id=pid,
            )
            if len(source_dossiers) != 1:
                return revision(
                    payload=kwargs["current_payload"],
                    validation_errors=(
                        "qualification_research_correction_source_context_missing",
                    ),
                    valid_item_keys=(),
                    agent_session_id=str(route.get("agent_session_id") or ""),
                    workspace_dir=str(route.get("workspace_dir") or ""),
                )
            source_dossier = source_dossiers[0]
            research_ready, _readiness_reasons = assess_research_readiness(
                source_dossier
            )
            if research_ready:
                evidence_verified, _evidence_errors = (
                    verify_persisted_research_evidence(source_dossier)
                )
                if evidence_verified:
                    # Stage completion is a monotonic boundary.  A later optioning or
                    # selection concern may correct that downstream author, but it cannot
                    # make an authenticated, verifier-clean research proof incomplete.  No
                    # model invocation is needed here; retain the exact dossier and advance.
                    document = dict(stage3)
                    document["items"] = [source_dossier]
                    meta_raw = document.get("input_meta")
                    meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
                    meta["qualification_research_completion_advance"] = {
                        "status": "already_complete",
                        "problem_id": pid,
                        "readiness_verified": True,
                        "persisted_evidence_verified": True,
                        "source": "current_correction_frontier",
                    }
                    document["input_meta"] = meta
                    candidate_docs[str(route["route_sha256"])] = document
                    return revision(
                        payload=source_dossier,
                        validation_errors=(),
                        valid_item_keys=("research:" + pid,),
                        agent_session_id=str(route.get("agent_session_id") or ""),
                        workspace_dir=str(route.get("workspace_dir") or ""),
                        receipt_evidence=meta[
                            "qualification_research_completion_advance"
                        ],
                    )
            if repo_input is None:
                return revision(
                    payload=kwargs["current_payload"],
                    validation_errors=(
                        "qualification_research_correction_source_context_missing",
                    ),
                    valid_item_keys=(),
                    agent_session_id=str(route.get("agent_session_id") or ""),
                    workspace_dir=str(route.get("workspace_dir") or ""),
                )
            findings = [
                f"independent_qualification_finding:{item}"
                for item in route.get("bad_categories", [])
                if isinstance(item, str) and item.strip()
            ] or [
                "independent_qualification_finding:"
                + str(route.get("rationale") or "semantic_research_failure")
            ]
            route_key = str(route["route_sha256"])
            retained_stage3 = candidate_docs.get(route_key, documents["repro_research"])
            objective_best_frontier = _qualification_research_objective_best_frontier(
                retained_stage3,
                problem_id=str(pid),
            )
            research_result = continue_research_dossier_from_independent_feedback(
                dossier=source_dossier,
                validation_errors=findings,
                repo_input=repo_input,
                requested_repo_ref=research_ref,
                resolved_repo_ref=research_ref,
                agent=agent,
                model=model,
                cfg=cfg,
                replay_timeout_seconds=replay_timeout_seconds,
                replay_executor=replay_executor,
                artifacts_dir=route_dir,
                independent_feedback=feedback,
                objective_best_frontier=objective_best_frontier,
            )
            if research_result.get("status") == "parked_external_wait":
                research_external_wait = research_result.get("external_wait")
                if isinstance(research_external_wait, Mapping):
                    raise BacklogProviderExternalWait(dict(research_external_wait))
            corrected_raw = research_result.get("dossier")
            corrected = dict(corrected_raw) if isinstance(corrected_raw, Mapping) else {}
            document = dict(stage3)
            document["items"] = [corrected] if corrected else []
            meta_raw = document.get("input_meta")
            meta = dict(meta_raw) if isinstance(meta_raw, Mapping) else {}
            meta["qualification_research_correction"] = {
                key: value
                for key, value in research_result.items()
                if key != "dossier"
            }
            document["input_meta"] = meta
            candidate_docs[route_key] = document
            errors = [
                str(error)
                for error in research_result.get("validation_errors", [])
                if str(error).strip()
            ]
            if research_result.get("status") != "corrected" and not errors:
                errors.append(
                    "qualification_research_correction_incomplete:"
                    + str(research_result.get("status"))
                )
            return revision(
                payload=corrected or kwargs["current_payload"],
                validation_errors=tuple(errors),
                valid_item_keys=(
                    ("research:" + pid,) if corrected and not errors else ()
                ),
                agent_session_id=str(route.get("agent_session_id") or ""),
                workspace_dir=str(route.get("workspace_dir") or ""),
                receipt_evidence=research_result,
            )
        if stage == "solution_optioning":
            document = _run_solution_optioning_stage(
                repo_root=repo_root,
                target_repo_roots_by_problem=roots,
                atoms=atoms,
                problem_records=_records_for_problem(problem_records, problem_id=pid),
                priority_decisions=_records_for_problem(priority_decisions, problem_id=pid),
                research_dossiers=_records_for_problem(research_dossiers, problem_id=pid),
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=route_dir,
                out_json=route_dir / "solution_options.json",
                out_md=route_dir / "solution_options.md",
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=False,
                breadth_profile=breadth_profile,
                stage_guidance_text=stage4_guidance,
                external_corrections_by_problem={pid: correction},
            )
        elif stage == "solution_selection":
            document = _run_solution_selection_stage(
                repo_root=repo_root,
                target_repo_roots_by_problem=roots,
                atoms=atoms,
                problem_records=_records_for_problem(problem_records, problem_id=pid),
                research_dossiers=_records_for_problem(research_dossiers, problem_id=pid),
                solution_options=_records_for_problem(solution_options, problem_id=pid),
                solution_optioning_stage_doc=dict(stage4),
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=route_dir,
                out_json=route_dir / "solution_selection.json",
                out_md=route_dir / "solution_selection.md",
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=False,
                breadth_profile=breadth_profile,
                stage_guidance_text=stage5_guidance,
                external_corrections_by_problem={pid: correction},
            )
        else:
            document = _run_implementation_planning_stage(
                repo_root=repo_root,
                target_repo_roots_by_problem=roots,
                problem_records=_records_for_problem(problem_records, problem_id=pid),
                research_dossiers=_records_for_problem(research_dossiers, problem_id=pid),
                solution_options=_records_for_problem(solution_options, problem_id=pid),
                selection_decisions=_records_for_problem(selection_decisions, problem_id=pid),
                pipeline_manifest=pipeline_manifest,
                artifacts_dir=route_dir,
                out_json=route_dir / "change_plans.json",
                out_md=route_dir / "change_plans.md",
                agent=agent,
                model=model,
                cfg=cfg,
                dry_run=False,
                stage_guidance_text=stage6_guidance,
                external_corrections_by_problem={pid: correction},
            )
        candidate_docs[str(route["route_sha256"])] = document
        session_id, workspace_dir, binding_errors = _author_binding_from_stage_doc(
            document,
            stage=stage,
            problem_id=pid,
        )
        errors = [
            *_stage_failure_errors(document, stage=stage, problem_id=str(pid)),
            *binding_errors,
        ]
        items = _items(document)
        return revision(
            payload=items[0] if len(items) == 1 else items,
            validation_errors=tuple(dict.fromkeys(errors)),
            valid_item_keys=tuple(
                sorted(
                    value
                    for item in items
                    for value in (
                        _text(item.get("option_id")),
                        _text(item.get("selected_option_id")),
                        _text(item.get("plan_revision_id")),
                    )
                    if value is not None
                )
            ),
            agent_session_id=session_id or "",
            workspace_dir=workspace_dir or "",
            receipt_evidence=document,
        )

    materialized_receipts: list[dict[str, Any]] = []

    def rerun_downstream(
        *,
        accepted_repairs: Sequence[Mapping[str, Any]],
        stages: Sequence[str],
    ) -> Mapping[str, Any]:
        nonlocal atoms, current_case_registry, problem_records, priority_decisions
        nonlocal research_dossiers, solution_options, selection_decisions
        materialized_receipts.clear()
        affected: set[str] = set()
        direct_stage_by_pid: dict[str, str] = {}
        stage_order = {
            "problem_mining": 1,
            "problem_prioritization": 2,
            "repro_research": 3,
            "solution_optioning": 4,
            "solution_selection": 5,
            "implementation_planning": 6,
        }
        stage1_component_order = {
            "problem_miner": 1,
            "coverage_review": 2,
            "relation_review": 3,
        }

        def frontier_rank(repair: Mapping[str, Any]) -> tuple[int, int]:
            route = repair["route"]
            stage = str(route["authoring_stage"])
            provenance = route.get("author_provenance")
            adapter = (
                _text(provenance.get("stage1_correction_adapter"))
                if isinstance(provenance, Mapping)
                else None
            )
            return (
                stage_order[stage],
                stage1_component_order.get(adapter or "", 0),
            )

        stage1_repairs = [
            repair
            for repair in accepted_repairs
            if _text(repair.get("route", {}).get("authoring_stage"))
            == "problem_mining"
        ]
        selected_stage1_repair = (
            min(
                stage1_repairs,
                key=lambda repair: (
                    frontier_rank(repair),
                    str(repair["route"]["route_sha256"]),
                ),
            )
            if stage1_repairs
            else None
        )
        retained_pending_stage1_composition = [
            {
                "route_sha256": repair["route"]["route_sha256"],
                "grouped_route_sha256s": [
                    route["route_sha256"]
                    for route in (
                        repair.get("routes")
                        if isinstance(repair.get("routes"), list)
                        else [repair["route"]]
                    )
                    if isinstance(route, Mapping)
                ],
                "candidate_stage_document_sha256": _hash(
                    candidate_docs.get(str(repair["route"]["route_sha256"]), {})
                ),
                "candidate_case_registry_sha256": _hash(
                    candidate_case_registries.get(
                        str(repair["route"]["route_sha256"]), {}
                    )
                ),
                "candidate_atom_corpus_sha256": _hash(
                    candidate_atoms.get(str(repair["route"]["route_sha256"]), [])
                ),
                "candidate_artifacts": dict(
                    candidate_docs.get(
                        str(repair["route"]["route_sha256"]), {}
                    ).get("artifacts", {})
                )
                if isinstance(
                    candidate_docs.get(
                        str(repair["route"]["route_sha256"]), {}
                    ).get("artifacts"),
                    Mapping,
                )
                else {},
                "route_consumption_receipt_sha256s": dict(
                    repair.get("route_consumption_receipt_sha256s", {})
                ),
                "disposition": "retained_pending_composition_revalidation",
                "reason": (
                    "Multiple independently corrected Stage-1 frontiers do not yet "
                    "have a proven receipt/case-graph composition contract. This "
                    "candidate and its exact-author receipts are retained for the next "
                    "fresh cycle instead of item-merging inconsistent canonical graphs."
                ),
            }
            for repair in stage1_repairs
            if repair is not selected_stage1_repair
        ]
        repairs_by_pid: dict[str, list[Mapping[str, Any]]] = {}
        for repair in accepted_repairs:
            route = repair["route"]
            stage = _text(route.get("authoring_stage"))
            if (
                stage == "problem_mining"
                and selected_stage1_repair is not None
                and repair is not selected_stage1_repair
            ):
                continue
            if stage == "problem_mining":
                route_key = str(route["route_sha256"])
                pids = candidate_problem_ids.get(route_key, set())
            else:
                repair_routes_raw = repair.get("routes")
                repair_routes = (
                    [
                        item
                        for item in repair_routes_raw
                        if isinstance(item, Mapping)
                    ]
                    if isinstance(repair_routes_raw, list)
                    else [route]
                )
                pids = {
                    pid
                    for item in repair_routes
                    for pid in [_route_problem_id(item)]
                    if pid is not None
                }
            if stage not in stage_order or not pids:
                continue
            for affected_pid in pids:
                repairs_by_pid.setdefault(affected_pid, []).append(repair)

        superseded_direct_repairs: list[dict[str, Any]] = []
        for pid, pid_repairs in repairs_by_pid.items():
            earliest_rank = min(frontier_rank(repair) for repair in pid_repairs)
            frontier_repairs = [
                repair
                for repair in pid_repairs
                if frontier_rank(repair) == earliest_rank
            ]
            winning_repair = frontier_repairs[-1]
            for superseded in pid_repairs:
                if superseded is winning_repair:
                    continue
                same_frontier = frontier_rank(superseded) == earliest_rank
                superseded_direct_repairs.append(
                    {
                        "problem_id": pid,
                        "route_sha256": superseded["route"]["route_sha256"],
                        "authoring_stage": superseded["route"]["authoring_stage"],
                        "disposition": (
                            "retained_revalidation_required_ambiguous_same_frontier"
                            if same_frontier
                            else "superseded_by_earlier_upstream_repair"
                        ),
                        "superseded_by_route_sha256": winning_repair["route"][
                            "route_sha256"
                        ],
                        "reason": (
                            (
                                "Multiple accepted repairs target an overlapping frontier "
                                "without a proven composition contract; retain the alternate "
                                "candidate and require revalidation."
                            )
                            if same_frontier
                            else (
                                "The accepted direct repair was authored against an upstream "
                                "frontier that changed; its stage is regenerated and must be "
                                "independently re-adjudicated."
                            )
                        ),
                    }
                )

            route = winning_repair["route"]
            route_key = str(route["route_sha256"])
            stage = str(route["authoring_stage"])
            affected.add(pid)
            direct_stage_by_pid[pid] = stage
            candidate = candidate_docs[route_key]
            documents[stage] = _merge_problem_items(
                documents[stage],
                candidate,
                problem_ids={pid},
                repair_receipts=[
                    {
                        "route_sha256": grouped_route["route_sha256"],
                        "consumption_receipt_sha256": winning_repair.get(
                            "route_consumption_receipt_sha256s", {}
                        ).get(
                            str(grouped_route["route_sha256"]),
                            winning_repair["route_consumption_receipt_sha256"],
                        ),
                    }
                    for grouped_route in (
                        winning_repair.get("routes")
                        if isinstance(winning_repair.get("routes"), list)
                        else [route]
                    )
                    if isinstance(grouped_route, Mapping)
                ],
            )
            if stage == "problem_mining":
                atoms = candidate_atoms[route_key]
                if route_key in candidate_case_registries:
                    current_case_registry = candidate_case_registries[route_key]
                    candidate_case_registries["current"] = current_case_registry

        problem_records = _items(documents["problem_mining"])
        priority_decisions = _items(documents["problem_prioritization"])
        research_dossiers = _items(documents["repro_research"])
        solution_options = _items(documents["solution_optioning"])
        selection_decisions = _items(documents["solution_selection"])

        # Recompute only stages strictly downstream of each direct repair.  A direct
        # exact-author result is never discarded by redundantly rerunning its own stage.
        for pid in sorted(affected):
            direct_stage = direct_stage_by_pid[pid]
            direct_stage_number = stage_order[direct_stage]
            pid_dir = repair_artifacts_dir / "downstream" / sha256(pid.encode()).hexdigest()[:16]
            pid_dir.mkdir(parents=True, exist_ok=True)
            if not _records_for_problem(problem_records, problem_id=pid):
                # Relation repair can merge/alias a prior canonical problem out of
                # existence. Remove every stale descendant instead of asking later
                # stages to invent output for an empty problem frontier.
                for downstream_stage in (
                    "problem_prioritization",
                    "repro_research",
                    "solution_optioning",
                    "solution_selection",
                    "implementation_planning",
                ):
                    original_document = documents[downstream_stage]
                    empty_replacement = dict(original_document)
                    empty_replacement["items"] = []
                    documents[downstream_stage] = _merge_problem_items(
                        original_document,
                        empty_replacement,
                        problem_ids={pid},
                        repair_receipts=[],
                    )
                priority_decisions = _items(documents["problem_prioritization"])
                research_dossiers = _items(documents["repro_research"])
                solution_options = _items(documents["solution_optioning"])
                selection_decisions = _items(documents["solution_selection"])
                continue
            if direct_stage_number < 2:
                problem_records = _items(documents["problem_mining"])
                new_stage2 = _run_problem_prioritization_stage(
                    atoms=atoms,
                    problem_records=_records_for_problem(
                        problem_records, problem_id=pid
                    ),
                    pipeline_manifest=pipeline_manifest,
                    artifacts_dir=pid_dir,
                    out_json=pid_dir / "prioritized_problems.json",
                    out_md=pid_dir / "prioritized_problems.md",
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    dry_run=False,
                    stage_guidance_text=stage2_guidance,
                )
                documents["problem_prioritization"] = _merge_problem_items(
                    documents["problem_prioritization"],
                    new_stage2,
                    problem_ids={pid},
                    repair_receipts=[],
                )
                priority_decisions = _items(documents["problem_prioritization"])
            if direct_stage_number < 3:
                if repo_input is None or research_ref is None or replay_executor is None:
                    raise ValueError(
                        "qualification_downstream_research_context_missing"
                    )
                new_stage3 = _run_repro_research_stage(
                    repo_root=repo_root,
                    repo_input=repo_input,
                    repo_ref=research_ref,
                    target_slug=target_slug,
                    selected_priority_decisions=_records_for_problem(
                        priority_decisions, problem_id=pid
                    ),
                    problem_records=_records_for_problem(
                        problem_records, problem_id=pid
                    ),
                    atoms=atoms,
                    artifacts_dir=pid_dir,
                    out_json=pid_dir / "research.json",
                    out_md=pid_dir / "research.md",
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    dry_run=False,
                    replay_timeout_seconds=float(replay_timeout_seconds or 10800.0),
                    replay_executor=replay_executor,
                    replay_executor_metadata=dict(replay_executor_metadata or {}),
                )
                new_stage3_external_wait = _stage3_external_wait(new_stage3)
                if new_stage3_external_wait is not None:
                    raise BacklogProviderExternalWait(new_stage3_external_wait)
                documents["repro_research"] = _merge_problem_items(
                    documents["repro_research"],
                    new_stage3,
                    problem_ids={pid},
                    repair_receipts=[],
                )
            research_dossiers = _items(documents["repro_research"])
            roots = target_roots({pid})
            if direct_stage_number < 4:
                new_stage4 = _run_solution_optioning_stage(
                    repo_root=repo_root,
                    target_repo_roots_by_problem=roots,
                    atoms=atoms,
                    problem_records=_records_for_problem(problem_records, problem_id=pid),
                    priority_decisions=_records_for_problem(
                        priority_decisions, problem_id=pid
                    ),
                    research_dossiers=_records_for_problem(
                        research_dossiers, problem_id=pid
                    ),
                    pipeline_manifest=pipeline_manifest,
                    artifacts_dir=pid_dir,
                    out_json=pid_dir / "solution_options.json",
                    out_md=pid_dir / "solution_options.md",
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    dry_run=False,
                    breadth_profile=breadth_profile,
                    stage_guidance_text=stage4_guidance,
                )
                documents["solution_optioning"] = _merge_problem_items(
                    documents["solution_optioning"],
                    new_stage4,
                    problem_ids={pid},
                    repair_receipts=[],
                )
            solution_options = _items(documents["solution_optioning"])
            if direct_stage_number < 5:
                new_stage5 = _run_solution_selection_stage(
                    repo_root=repo_root,
                    target_repo_roots_by_problem=roots,
                    atoms=atoms,
                    problem_records=_records_for_problem(problem_records, problem_id=pid),
                    research_dossiers=_records_for_problem(research_dossiers, problem_id=pid),
                    solution_options=_records_for_problem(solution_options, problem_id=pid),
                    solution_optioning_stage_doc=documents["solution_optioning"],
                    pipeline_manifest=pipeline_manifest,
                    artifacts_dir=pid_dir,
                    out_json=pid_dir / "solution_selection.json",
                    out_md=pid_dir / "solution_selection.md",
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    dry_run=False,
                    breadth_profile=breadth_profile,
                    stage_guidance_text=stage5_guidance,
                )
                documents["solution_selection"] = _merge_problem_items(
                    documents["solution_selection"],
                    new_stage5,
                    problem_ids={pid},
                    repair_receipts=[],
                )
            selection_decisions = _items(documents["solution_selection"])
            if direct_stage_number < 6:
                new_stage6 = _run_implementation_planning_stage(
                    repo_root=repo_root,
                    target_repo_roots_by_problem=roots,
                    problem_records=_records_for_problem(problem_records, problem_id=pid),
                    research_dossiers=_records_for_problem(research_dossiers, problem_id=pid),
                    solution_options=_records_for_problem(solution_options, problem_id=pid),
                    selection_decisions=_records_for_problem(
                        selection_decisions, problem_id=pid
                    ),
                    pipeline_manifest=pipeline_manifest,
                    artifacts_dir=pid_dir,
                    out_json=pid_dir / "change_plans.json",
                    out_md=pid_dir / "change_plans.md",
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    dry_run=False,
                    stage_guidance_text=stage6_guidance,
                )
                documents["implementation_planning"] = _merge_problem_items(
                    documents["implementation_planning"],
                    new_stage6,
                    problem_ids={pid},
                    repair_receipts=[],
                )

        for stage, document in documents.items():
            path = repair_artifacts_dir / "materialized" / f"{stage}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            materialized_receipts.append(
                {
                    "stage": stage,
                    "path": str(path.resolve()),
                    "sha256": sha256(path.read_bytes()).hexdigest(),
                    "content_sha256": _hash(document),
                }
            )
        if isinstance(current_case_registry, dict):
            registry_path = repair_artifacts_dir / "materialized" / "case_registry.json"
            registry_path.write_text(
                json.dumps(
                    current_case_registry,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            materialized_receipts.append(
                {
                    "stage": "case_registry",
                    "path": str(registry_path.resolve()),
                    "sha256": sha256(registry_path.read_bytes()).hexdigest(),
                    "content_sha256": _hash(current_case_registry),
                }
            )
            stage1_artifacts_raw = documents["problem_mining"].get("artifacts")
            stage1_artifacts = (
                stage1_artifacts_raw
                if isinstance(stage1_artifacts_raw, Mapping)
                else {}
            )
            artifacts_by_problem_raw = stage1_artifacts.get(
                "qualification_repair_artifacts_by_problem"
            )
            artifacts_by_problem = (
                artifacts_by_problem_raw
                if isinstance(artifacts_by_problem_raw, Mapping)
                else {}
            )
            evidence_candidates = [
                stage1_artifacts.get(
                    "qualification_repair_current_evidence_receipt"
                ),
                *[
                scoped_artifacts.get("problem_mining_evidence_receipt")
                for scoped_artifacts in reversed(list(artifacts_by_problem.values()))
                if isinstance(scoped_artifacts, Mapping)
                and scoped_artifacts.get("problem_mining_evidence_receipt")
                ],
                stage1_artifacts.get("problem_mining_evidence_receipt")
            ]
            evidence_path = next(
                (
                    path
                    for value in evidence_candidates
                    for path in [Path(str(value or ""))]
                    if path.is_file()
                ),
                Path(""),
            )
            if evidence_path.is_file():
                materialized_receipts.append(
                    {
                        "stage": "problem_mining_evidence",
                        "path": str(evidence_path.resolve()),
                        "sha256": sha256(evidence_path.read_bytes()).hexdigest(),
                        "content_sha256": _hash(
                            json.loads(evidence_path.read_text(encoding="utf-8"))
                        ),
                    }
                )
        return {
            "affected_problem_ids": sorted(affected),
            "requested_downstream_stages": list(stages),
            "superseded_direct_repairs": superseded_direct_repairs,
            "retained_pending_stage1_composition": (
                retained_pending_stage1_composition
            ),
            "materialized_stage_receipts": materialized_receipts,
        }

    route_by_sha = {str(route["route_sha256"]): route for route in routes}
    execution_consumptions: list[dict[str, Any]] = []
    executed_plan_groups: list[dict[str, Any]] = []
    executed_group_ids: set[str] = set()
    provider_external_wait: dict[str, Any] | None = None
    # One earliest author frontier owns each causal component for this scoring pass.
    # Recompute after every completed group because accepted Stage-1 work can create a
    # merge/alias edge that makes a previously disjoint downstream author stale. A
    # provider-global wait is different from an author-local pause: it stops this whole
    # scoring pass immediately, retaining every later group as pending without dispatch.
    while True:
        route_plan = plan_qualification_repair_route_groups(
            routes,
            stage1=documents["problem_mining"],
            case_registry=current_case_registry,
        )
        planned_group = next(
            (
                group
                for group in route_plan
                if group["disposition"] == "selected_causal_frontier"
                and group["group_id"] not in executed_group_ids
            ),
            None,
        )
        if planned_group is None:
            break
        group_routes = [
            route_by_sha[route_sha]
            for route_sha in planned_group["route_sha256s"]
        ]
        try:
            execution = consume_qualification_corrections(
                routes=group_routes,
                source_pending_run_sha256=source_pending_run_sha256,
                source_adjudication_sha256=source_adjudication_sha256,
                load_current_payload=current_payload,
                invoke_exact_author=invoke_exact_author,
                rerun_downstream=rerun_downstream,
                resume_frontiers={
                    route_sha: frontier
                    for route_sha, frontier in (resume_frontiers or {}).items()
                    if route_sha in set(planned_group["route_sha256s"])
                },
            )
        except BacklogProviderExternalWait as exc:
            provider_external_wait = dict(exc.external_wait)
            break
        execution_consumptions.append(execution)
        executed_plan_groups.append(planned_group)
        executed_group_ids.add(str(planned_group["group_id"]))

    route_plan = plan_qualification_repair_route_groups(
        routes,
        stage1=documents["problem_mining"],
        case_registry=current_case_registry,
    )
    pending_plan_groups = [
        group
        for group in route_plan
        if group["group_id"] not in executed_group_ids
    ]
    pending_routes = [
        route_by_sha[route_sha]
        for group in pending_plan_groups
        for route_sha in group["route_sha256s"]
    ]
    pending_group_sha_by_route = {
        route_sha: list(group["route_sha256s"])
        for group in pending_plan_groups
        for route_sha in group["route_sha256s"]
    }
    pending_plan_by_route = {
        route_sha: group
        for group in pending_plan_groups
        for route_sha in group["route_sha256s"]
    }
    receipt_by_route: dict[str, dict[str, Any]] = {
        str(receipt["route_sha256"]): dict(receipt)
        for execution in execution_consumptions
        for receipt in execution.get("route_receipts", [])
        if isinstance(receipt, Mapping)
    }
    for route in pending_routes:
        feedback = correction_feedback_document(
            route,
            source_pending_run_sha256=source_pending_run_sha256,
            source_adjudication_sha256=source_adjudication_sha256,
        )
        receipt = {
            "route_sha256": route["route_sha256"],
            "feedback_sha256": feedback["content_sha256"],
            "grouped_route_sha256s": pending_group_sha_by_route[
                str(route["route_sha256"])
            ],
            "causal_component_id": pending_plan_by_route[
                str(route["route_sha256"])
            ]["component_id"],
            "blocked_by_group_id": pending_plan_by_route[
                str(route["route_sha256"])
            ]["blocked_by_group_id"],
            "status": "retained_pending_not_invoked",
            "reason": (
                "A causally earlier author frontier must be independently re-adjudicated "
                "before this retained route can be corrected. No model invocation was made."
            ),
            "authored_work_disposition": "retained",
            "attempts": [],
            "assessments": [],
            "invocation_failures": [],
            "current_payload_sha256": None,
            "best_payload_sha256": None,
            "accepted_payload_sha256": None,
            "rerun_downstream_stages": [],
        }
        receipt["content_sha256"] = _hash(receipt)
        receipt_by_route[str(route["route_sha256"])] = receipt

    affected_problem_ids = sorted(
        {
            value
            for execution in execution_consumptions
            for value in execution.get("downstream_result", {}).get(
                "affected_problem_ids", []
            )
            if isinstance(value, str) and value.strip()
        }
    )
    requested_downstream_stages = list(
        dict.fromkeys(
            stage
            for execution in execution_consumptions
            for stage in execution.get("rerun_downstream_stages", [])
            if isinstance(stage, str) and stage.strip()
        )
    )
    downstream_results = [
        dict(execution.get("downstream_result", {}))
        for execution in execution_consumptions
        if isinstance(execution.get("downstream_result"), Mapping)
        and execution.get("downstream_result")
    ]
    final_materialized_receipts = (
        list(downstream_results[-1].get("materialized_stage_receipts", []))
        if downstream_results
        else []
    )
    downstream_result = {
        "affected_problem_ids": affected_problem_ids,
        "requested_downstream_stages": requested_downstream_stages,
        "superseded_direct_repairs": [
            item
            for result in downstream_results
            for item in result.get("superseded_direct_repairs", [])
            if isinstance(item, Mapping)
        ],
        "materialized_stage_receipts": final_materialized_receipts,
        "retained_pending_not_invoked": [
            {
                "route_sha256": route["route_sha256"],
                "grouped_route_sha256s": pending_group_sha_by_route[
                    str(route["route_sha256"])
                ],
                "status": "retained_pending_not_invoked",
            }
            for route in pending_routes
        ],
        "stage1_group_attempt_count": sum(
            any(
                _text(route_by_sha[route_sha].get("authoring_stage"))
                == "problem_mining"
                for route_sha in group["route_sha256s"]
            )
            for group in executed_plan_groups
        ),
        "stage1_group_accepted": any(
            int(execution.get("accepted_repair_count") or 0) > 0
            and any(
                _text(route_by_sha[str(receipt["route_sha256"])].get("authoring_stage"))
                == "problem_mining"
                for receipt in execution.get("route_receipts", [])
                if isinstance(receipt, Mapping)
            )
            for execution in execution_consumptions
        ),
        "route_group_plan": route_plan,
        "external_wait": provider_external_wait,
    }
    accepted_repair_count = sum(
        int(execution.get("accepted_repair_count") or 0)
        for execution in execution_consumptions
    )
    consumption = {
        "schema_version": 1,
        "contract_kind": "qualification_correction_consumption",
        "source_pending_run_sha256": source_pending_run_sha256,
        "source_adjudication_sha256": source_adjudication_sha256,
        "route_set_sha256": _hash([route["route_sha256"] for route in routes]),
        "route_receipts": [
            receipt_by_route[str(route["route_sha256"])] for route in routes
        ],
        "accepted_repair_count": accepted_repair_count,
        "accepted_repair_group_count": sum(
            int(execution.get("accepted_repair_group_count") or 0)
            for execution in execution_consumptions
        ),
        "unresolved_route_count": len(routes) - accepted_repair_count,
        "pending_not_invoked_route_count": len(pending_routes),
        "pending_not_invoked_group_count": len(pending_plan_groups),
        "rerun_downstream_stages": requested_downstream_stages,
        "downstream_result": downstream_result,
        "downstream_result_sha256": _hash(downstream_result),
        "same_corpus_feedback_exposed": True,
        "release_qualification_eligible": False,
        "fresh_independent_readjudication_required": True,
        "status": (
            "parked_external_wait" if provider_external_wait is not None else "completed"
        ),
        "external_wait": provider_external_wait,
    }
    consumption["content_sha256"] = _hash(consumption)
    tickets = assemble_backlog_tickets(
        problem_records=_items(documents["problem_mining"]),
        priority_decisions=_items(documents["problem_prioritization"]),
        research_dossiers=_items(documents["repro_research"]),
        solution_option_sets=_items(documents["solution_optioning"]),
        selection_decisions=_items(documents["solution_selection"]),
        change_plans=_items(documents["implementation_planning"]),
    )
    return QualificationRepairRuntimeResult(
        consumption=consumption,
        stage_documents=documents,
        tickets=tickets,
        affected_problem_ids=list(
            consumption.get("downstream_result", {}).get("affected_problem_ids", [])
        ),
        atoms=atoms,
        case_registry=current_case_registry,
    )


__all__ = [
    "QualificationRepairRuntimeResult",
    "plan_qualification_repair_route_groups",
    "run_stage456_qualification_repairs",
]
