"""Stage-aware candidate neighborhood ranking and relation-decision application.

This module wraps ``triage_engine`` to produce *candidate neighborhoods* at each
pipeline stage.  A neighborhood exposes signal families separately so the caller can
render "most related by semantics," "most related by overlap," etc. to a human reviewer.

Design contract
---------------
- Automatic logic is **supplementary and exclusion-oriented**.  This module computes
  neighborhoods; it never decides merge, split, or grouping on its own.
- The ``rank_stage_related_items`` output must preserve separate per-signal neighborhoods.
- ``apply_relation_decisions`` applies an explicit list of reviewer decisions.  It does
  not silently infer decisions from similarity scores.
- All operations are deterministic given the same inputs and the same ``embedder``.

Split hints
-----------
A split hint is a deterministic clue that a single item may bundle multiple issues.
Indicators include disjoint atom sets across the item's sub-evidence groups, or multiple
unrelated path anchors with no shared prefix.  Split hints are advisory; the reviewer
decides whether to act on them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any, Protocol

from backlog_core.case_lineage import (
    mint_case_id,
    provisional_same_cause_clearance_errors,
    provisional_same_cause_group_errors,
)

_LOG = logging.getLogger(__name__)

# Sentinel type for the embedder protocol so this module can function without triage_engine
# being installed.  When triage_engine is available, it will satisfy the protocol.
class _Embedder(Protocol):
    def embed_texts(self, texts: list[str]) -> list[tuple[float, ...]]:
        ...


# ---------------------------------------------------------------------------
# Relation-review config helpers
# ---------------------------------------------------------------------------

_VALID_STAGES: frozenset[str] = frozenset(
    {
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
    }
)

_VALID_ACTIONS: frozenset[str] = frozenset(
    {"merge", "keep_separate", "split", "same_cause_group", "alias"}
)


def _runner_owned_same_cause_group_id(
    member_case_ids: Sequence[str],
    *,
    provisional: bool,
) -> str:
    """Mint a stable runner-owned ID for an exact same-cause member set.

    ``group_id`` is model-authored naming, not causal evidence. Independent review
    batches can agree on the exact member set while choosing different labels. The
    operational unit therefore binds its identity to the stable case members instead
    of requiring nondeterministic prose-derived IDs to match byte-for-byte.  The status
    prefix is descriptive only; causal verification remains a separate contract.
    """

    members = sorted(set(member_case_ids))
    digest = sha256("\0".join(members).encode("utf-8")).hexdigest()[:20]
    status = "provisional" if provisional else "canonical"
    return f"cause:{status}:{digest}"


def _stage_config(relation_config: dict[str, Any], stage: str) -> dict[str, Any]:
    """Return the effective relation-review config for *stage*.

    Merges stage-specific overrides on top of the defaults block.

    Parameters
    ----------
    relation_config:
        Parsed ``configs/backlog_relation_review.yaml`` content.
    stage:
        Stage identifier string.

    Returns
    -------
    dict[str, Any]
        Effective config for the stage.
    """
    defaults: dict[str, Any] = relation_config.get("defaults") or {}
    stages_block: dict[str, Any] = relation_config.get("stages") or {}
    override: dict[str, Any] = stages_block.get(stage) or {}
    return {**defaults, **override}


# ---------------------------------------------------------------------------
# Item text extraction
# ---------------------------------------------------------------------------


def _item_title(item: dict[str, Any]) -> str:
    """Return a usable title string for *item*."""
    for key in ("title", "problem_id", "option_id", "change_plan_id", "summary"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _item_text_chunks(item: dict[str, Any]) -> list[str]:
    """Return text chunks for *item* suitable for embedding."""
    chunks: list[str] = []
    for key in (
        "title",
        "problem",
        "user_impact",
        "evidence_summary",
        "summary",
        "tradeoffs",
        "rationale",
        "selection_rationale",
        "root_cause_hypotheses",
        "unknowns",
    ):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            chunks.append(val.strip())
        elif isinstance(val, list):
            chunks.extend(
                str(v).strip() for v in val if isinstance(v, (str, dict)) and str(v).strip()
            )
    return chunks


def _item_evidence_ids(item: dict[str, Any]) -> list[str]:
    """Return evidence atom ID list for *item*."""
    for key in ("evidence_atom_ids", "evidence_atom_ids_used"):
        val = item.get(key)
        if isinstance(val, list):
            return [str(v) for v in val if isinstance(v, str) and v.strip()]
    return []


def _item_focus_id(item: dict[str, Any]) -> str:
    """Return a stable focus ID for *item*."""
    for key in (
        "problem_id",
        "option_id",
        "change_plan_id",
        "selected_option_id",
    ):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    title = _item_title(item)
    return title or "(unknown)"


# ---------------------------------------------------------------------------
# Split-hint detection
# ---------------------------------------------------------------------------


def _compute_split_hints(item: dict[str, Any], *, disjoint_fraction: float) -> list[dict[str, Any]]:
    """Compute deterministic split hints for *item*.

    A split hint is emitted when evidence atom IDs span distinct run prefixes and
    the disjoint fraction between the two largest groups exceeds *disjoint_fraction*.

    Parameters
    ----------
    item:
        Stage item dict.
    disjoint_fraction:
        Minimum fraction of disjoint evidence required to emit a hint.

    Returns
    -------
    list[dict[str, Any]]
        List of split hint dicts.  May be empty.
    """
    eids = _item_evidence_ids(item)
    if len(eids) < 2:
        return []

    # Group by run prefix (first two path segments: target/timestamp).
    groups: dict[str, list[str]] = {}
    for eid in eids:
        parts = eid.split("/")
        prefix = "/".join(parts[:2]) if len(parts) >= 2 else parts[0] if parts else "_"
        groups.setdefault(prefix, []).append(eid)

    if len(groups) < 2:
        return []

    # Find the two largest groups and compute Jaccard disjointness.
    sorted_groups = sorted(groups.values(), key=len, reverse=True)
    g1 = frozenset(sorted_groups[0])
    g2 = frozenset(sorted_groups[1])
    intersection = len(g1 & g2)
    union = len(g1 | g2)
    jaccard = intersection / union if union else 0.0
    disjoint = 1.0 - jaccard

    if disjoint < float(disjoint_fraction):
        return []

    hints: list[dict[str, Any]] = [
        {
            "hint_type": "disjoint_evidence_groups",
            "disjoint_fraction": round(disjoint, 4),
            "group_a_size": len(g1),
            "group_b_size": len(g2),
            "group_a_prefix": sorted(groups.keys())[0],
            "group_b_prefix": sorted(groups.keys())[1],
            "note": (
                f"Evidence atoms span {len(groups)} distinct run prefixes with "
                f"{disjoint:.0%} disjointness.  This item may bundle multiple issues."
            ),
        }
    ]
    return hints


# ---------------------------------------------------------------------------
# Neighborhood building
# ---------------------------------------------------------------------------


def _build_neighborhoods_no_embedder(
    items: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    stage: str,
) -> list[dict[str, Any]]:
    """Build candidate neighborhoods using only lexical signals (no embedder).

    When no embedder is available, semantic similarity is skipped and neighborhoods
    are built from evidence overlap and title-token overlap only.

    Parameters
    ----------
    items:
        Stage items.
    cfg:
        Effective relation-review config for the stage.
    stage:
        Stage identifier string.

    Returns
    -------
    list[dict[str, Any]]
        One neighborhood dict per item in *items*.
    """
    from triage_engine.text import tokenize

    top_k_ev = int(cfg.get("top_k_by_evidence_overlap", 3))
    top_k_meta = int(cfg.get("top_k_by_metadata", 2))
    disjoint_frac = float(cfg.get("split_hint_min_disjoint_evidence_fraction", 0.70))

    # Pre-compute token sets and evidence ID sets.
    token_sets: list[frozenset[str]] = [
        frozenset(tokenize(_item_title(it))) for it in items
    ]
    evidence_sets: list[frozenset[str]] = [
        frozenset(_item_evidence_ids(it)) for it in items
    ]

    neighborhoods: list[dict[str, Any]] = []

    for i, item in enumerate(items):
        fid = _item_focus_id(item)
        ev_i = evidence_sets[i]
        tok_i = token_sets[i]

        # Evidence overlap neighbors.
        ev_scores: list[tuple[float, int]] = []
        for j, ev_j in enumerate(evidence_sets):
            if j == i:
                continue
            if not ev_i or not ev_j:
                continue
            overlap = len(ev_i & ev_j)
            if overlap > 0:
                ev_scores.append((overlap, j))
        ev_scores.sort(key=lambda x: (-x[0], x[1]))
        ev_neighbors = [
            {
                "item_id": _item_focus_id(items[j]),
                "evidence_overlap": int(score),
                "index": j,
            }
            for score, j in ev_scores[:top_k_ev]
        ]

        # Metadata (title-token) neighbors.
        meta_scores: list[tuple[float, int]] = []
        for j, tok_j in enumerate(token_sets):
            if j == i:
                continue
            if not tok_i or not tok_j:
                continue
            inter = len(tok_i & tok_j)
            union = len(tok_i | tok_j)
            jac = inter / union if union else 0.0
            if jac > 0.0:
                meta_scores.append((jac, j))
        meta_scores.sort(key=lambda x: (-x[0], x[1]))
        meta_neighbors = [
            {
                "item_id": _item_focus_id(items[j]),
                "title_jaccard": round(score, 4),
                "index": j,
            }
            for score, j in meta_scores[:top_k_meta]
        ]

        split_hints = _compute_split_hints(item, disjoint_fraction=disjoint_frac)

        neighborhoods.append(
            {
                "focus_id": fid,
                "stage": stage,
                "most_related_by_semantic": [],
                "most_related_by_evidence_overlap": ev_neighbors,
                "most_related_by_metadata": meta_neighbors,
                "most_related_by_path_anchor": [],
                "split_hints": split_hints,
                "_embedder_available": False,
            }
        )

    return neighborhoods


def _build_neighborhoods_with_embedder(
    items: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    stage: str,
    embedder: _Embedder,
) -> list[dict[str, Any]]:
    """Build candidate neighborhoods using the full triage_engine signal set.

    Parameters
    ----------
    items:
        Stage items.
    cfg:
        Effective relation-review config for the stage.
    stage:
        Stage identifier string.
    embedder:
        Embedding backend implementing the ``_Embedder`` protocol.

    Returns
    -------
    list[dict[str, Any]]
        One neighborhood dict per item in *items*.
    """
    from triage_engine.similarity import build_item_vectors, compute_pair_similarity

    top_k_sem = int(cfg.get("top_k_by_semantic", 3))
    top_k_ev = int(cfg.get("top_k_by_evidence_overlap", 3))
    top_k_meta = int(cfg.get("top_k_by_metadata", 2))
    top_k_anchor = int(cfg.get("top_k_by_path_anchor", 2))
    min_sem = float(cfg.get("min_semantic_similarity", 0.55))
    disjoint_frac = float(cfg.get("split_hint_min_disjoint_evidence_fraction", 0.70))

    vectors = build_item_vectors(
        items,
        get_title=_item_title,
        get_text_chunks=_item_text_chunks,
        get_evidence_ids=_item_evidence_ids,
        embedder=embedder,  # type: ignore[arg-type]
    )

    n = len(items)
    neighborhoods: list[dict[str, Any]] = []

    for i, item in enumerate(items):
        fid = _item_focus_id(item)
        vi = vectors[i]

        sem_scores: list[tuple[float, int]] = []
        ev_scores: list[tuple[float, int]] = []
        meta_scores: list[tuple[float, int]] = []
        anchor_scores: list[tuple[float, int]] = []

        for j in range(n):
            if j == i:
                continue
            vj = vectors[j]
            sim = compute_pair_similarity(vi, vj)

            if sim.overall_similarity >= min_sem:
                sem_scores.append((sim.overall_similarity, j))
            if sim.evidence_overlap > 0:
                ev_scores.append((float(sim.evidence_overlap), j))
            if sim.title_jaccard > 0.0:
                meta_scores.append((sim.title_jaccard, j))
            if sim.anchor_jaccard > 0.0:
                anchor_scores.append((sim.anchor_jaccard, j))

        sem_scores.sort(key=lambda x: (-x[0], x[1]))
        ev_scores.sort(key=lambda x: (-x[0], x[1]))
        meta_scores.sort(key=lambda x: (-x[0], x[1]))
        anchor_scores.sort(key=lambda x: (-x[0], x[1]))

        sem_neighbors = [
            {
                "item_id": _item_focus_id(items[j]),
                "overall_similarity": round(score, 4),
                "index": j,
            }
            for score, j in sem_scores[:top_k_sem]
        ]
        ev_neighbors = [
            {
                "item_id": _item_focus_id(items[j]),
                "evidence_overlap": int(score),
                "index": j,
            }
            for score, j in ev_scores[:top_k_ev]
        ]
        meta_neighbors = [
            {
                "item_id": _item_focus_id(items[j]),
                "title_jaccard": round(score, 4),
                "index": j,
            }
            for score, j in meta_scores[:top_k_meta]
        ]
        anchor_neighbors = [
            {
                "item_id": _item_focus_id(items[j]),
                "anchor_jaccard": round(score, 4),
                "index": j,
            }
            for score, j in anchor_scores[:top_k_anchor]
        ]

        split_hints = _compute_split_hints(item, disjoint_fraction=disjoint_frac)

        neighborhoods.append(
            {
                "focus_id": fid,
                "stage": stage,
                "most_related_by_semantic": sem_neighbors,
                "most_related_by_evidence_overlap": ev_neighbors,
                "most_related_by_metadata": meta_neighbors,
                "most_related_by_path_anchor": anchor_neighbors,
                "split_hints": split_hints,
                "_embedder_available": True,
            }
        )

    return neighborhoods


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rank_stage_related_items(
    items: Sequence[dict[str, Any]],
    *,
    stage: str,
    relation_config: dict[str, Any],
    embedder: _Embedder | None = None,
) -> list[dict[str, Any]]:
    """Rank candidate neighborhoods for *items* at *stage*.

    Returns one neighborhood dict per item in *items*.  Each neighborhood preserves
    separate per-signal neighbor lists so the caller can render them independently.
    This function never makes merge/split/grouping decisions.

    Parameters
    ----------
    items:
        Stage items (problem records, research dossiers, etc.) as plain dicts.
    stage:
        Stage identifier string (one of the six pipeline stage IDs).
    relation_config:
        Parsed ``configs/backlog_relation_review.yaml`` content.
    embedder:
        Optional embedding backend.  When ``None``, semantic neighborhoods are
        omitted and only lexical signals are used.

    Returns
    -------
    list[dict[str, Any]]
        One neighborhood dict per item in *items*, in the same order.

    Raises
    ------
    ValueError
        When *stage* is not a known stage identifier.
    """
    if stage not in _VALID_STAGES:
        raise ValueError(
            f"rank_stage_related_items: unknown stage {stage!r}; "
            f"valid stages: {sorted(_VALID_STAGES)}"
        )

    items_list = list(items)
    if not items_list:
        return []

    cfg = _stage_config(relation_config, stage)
    _LOG.debug(
        "rank_stage_related_items: stage=%s items=%d embedder=%s",
        stage,
        len(items_list),
        "yes" if embedder is not None else "no",
    )

    if embedder is not None:
        try:
            return _build_neighborhoods_with_embedder(
                items_list, cfg=cfg, stage=stage, embedder=embedder
            )
        except Exception as exc:  # pragma: no cover
            _LOG.warning(
                "rank_stage_related_items: embedder failed (%s); "
                "falling back to lexical-only neighborhoods",
                exc,
            )

    return _build_neighborhoods_no_embedder(items_list, cfg=cfg, stage=stage)


def apply_relation_decisions(
    items: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    stage: str,
) -> list[dict[str, Any]]:
    """Apply a list of explicit relation-review decisions to *items*.

    Decisions are supplied by the reviewer prompt output.  This function applies
    them deterministically but does NOT infer additional decisions from similarity
    scores.

    Supported actions (``decision["action"]``):
    - ``merge``: Merge all ``target_ids`` into ``focus_id``.  The focus item absorbs
      the evidence atom IDs from the targets.  Target items are removed from the
      output.
    - ``keep_separate``: No action taken.  Recorded for audit purposes.
    - ``split``: Mark the focus item with a ``_split_hint_acknowledged`` flag and a
      ``split_rationale``.  Actual splitting into multiple items is a caller
      responsibility; this function only annotates.
    - ``same_cause_group``: Add ``same_cause_group_id`` to all listed items.
    - ``alias``: Mark the focus item as an alias of ``alias_target_id``.

    Parameters
    ----------
    items:
        Stage items (mutable; this function returns a new list).
    decisions:
        Reviewer decisions, each a dict with at minimum an ``action`` key and
        a ``focus_id`` key.
    stage:
        Stage identifier string, used for logging.

    Returns
    -------
    list[dict[str, Any]]
        Updated item list.  Order is preserved; merged-away items are removed.

    Raises
    ------
    ValueError
        When a decision contains an unknown ``action``.
    """
    if not decisions:
        return list(items)

    # Build a mutable index by focus ID.
    by_focus_id: dict[str, dict[str, Any]] = {}
    for item in items:
        fid = _item_focus_id(item)
        by_focus_id[fid] = dict(item)

    removed_ids: set[str] = set()

    for decision in decisions:
        action = str(decision.get("action", "")).strip()
        focus_id = str(decision.get("focus_id", "")).strip()
        rationale = str(decision.get("rationale", "")).strip()

        if not action:
            _LOG.warning("apply_relation_decisions: decision missing 'action' key; skipping")
            continue
        if action not in _VALID_ACTIONS:
            raise ValueError(
                f"apply_relation_decisions: unknown action {action!r} in stage={stage}; "
                f"valid actions: {sorted(_VALID_ACTIONS)}"
            )

        if action == "keep_separate":
            # Nothing to do except record.
            _LOG.debug(
                "apply_relation_decisions: stage=%s keep_separate focus=%s", stage, focus_id
            )

        elif action == "merge":
            target_ids = [
                str(t).strip()
                for t in (decision.get("target_ids") or [])
                if str(t).strip()
            ]
            focus_item = by_focus_id.get(focus_id)
            if focus_item is None:
                _LOG.warning(
                    "apply_relation_decisions: merge focus_id=%s not found; skipping",
                    focus_id,
                )
                continue
            merged_evidence: list[str] = list(focus_item.get("evidence_atom_ids") or [])
            merged_source_evidence: list[str] = list(
                focus_item.get("source_evidence_atom_ids") or []
            )
            merged_derived_evidence: list[str] = list(
                focus_item.get("derived_evidence_atom_ids") or []
            )
            for tid in target_ids:
                target_item = by_focus_id.get(tid)
                if target_item is None:
                    _LOG.warning(
                        "apply_relation_decisions: merge target_id=%s not found; skipping",
                        tid,
                    )
                    continue
                merged_evidence.extend(target_item.get("evidence_atom_ids") or [])
                merged_source_evidence.extend(
                    target_item.get("source_evidence_atom_ids") or []
                )
                merged_derived_evidence.extend(
                    target_item.get("derived_evidence_atom_ids") or []
                )
                removed_ids.add(tid)
            # Deduplicate evidence IDs while preserving order.
            seen: set[str] = set()
            deduped: list[str] = []
            for eid in merged_evidence:
                if eid not in seen:
                    seen.add(eid)
                    deduped.append(eid)
            focus_item["evidence_atom_ids"] = deduped
            focus_item["source_evidence_atom_ids"] = list(
                dict.fromkeys(merged_source_evidence)
            )
            focus_item["derived_evidence_atom_ids"] = list(
                dict.fromkeys(merged_derived_evidence)
            )
            focus_item["_merged_from"] = target_ids
            focus_item["_merge_rationale"] = rationale
            by_focus_id[focus_id] = focus_item
            _LOG.info(
                "apply_relation_decisions: stage=%s merged %s into %s",
                stage,
                target_ids,
                focus_id,
            )

        elif action == "split":
            focus_item = by_focus_id.get(focus_id)
            if focus_item is None:
                _LOG.warning(
                    "apply_relation_decisions: split focus_id=%s not found; skipping",
                    focus_id,
                )
                continue
            focus_item["_split_hint_acknowledged"] = True
            focus_item["_split_rationale"] = rationale
            by_focus_id[focus_id] = focus_item
            _LOG.info(
                "apply_relation_decisions: stage=%s split annotation on %s",
                stage,
                focus_id,
            )

        elif action == "same_cause_group":
            group_id = str(decision.get("group_id", focus_id)).strip()
            all_ids = [focus_id] + [
                str(t).strip()
                for t in (decision.get("member_ids") or [])
                if str(t).strip()
            ]
            for iid in all_ids:
                group_item = by_focus_id.get(iid)
                if group_item is None:
                    _LOG.warning(
                        "apply_relation_decisions: same_cause_group id=%s not found; skipping",
                        iid,
                    )
                    continue
                group_item["same_cause_group_id"] = group_id
                by_focus_id[iid] = group_item
            _LOG.info(
                "apply_relation_decisions: stage=%s same_cause_group group=%s members=%s",
                stage,
                group_id,
                all_ids,
            )

        elif action == "alias":
            alias_target = str(decision.get("alias_target_id", "")).strip()
            focus_item = by_focus_id.get(focus_id)
            if focus_item is None:
                _LOG.warning(
                    "apply_relation_decisions: alias focus_id=%s not found; skipping",
                    focus_id,
                )
                continue
            focus_item["_alias_of"] = alias_target
            focus_item["_alias_rationale"] = rationale
            by_focus_id[focus_id] = focus_item
            _LOG.info(
                "apply_relation_decisions: stage=%s alias %s → %s",
                stage,
                focus_id,
                alias_target,
            )

    # Reassemble list in original order, skipping removed items.
    result: list[dict[str, Any]] = []
    for item in items:
        fid = _item_focus_id(item)
        if fid in removed_ids:
            continue
        result.append(by_focus_id.get(fid, item))

    return result


def _clean_relation_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _clean_relation_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    )


def _relation_split_groups(
    item: dict[str, Any], decision: dict[str, Any]
) -> list[list[str]]:
    """Validate explicit evidence partitions for a split decision."""

    evidence_ids = _clean_relation_string_list(item.get("evidence_atom_ids"))
    explicit_raw = decision.get("split_groups")
    if not isinstance(explicit_raw, list) or not explicit_raw:
        raise ValueError(
            "canonicalize_problem_cases: split requires explicit split_groups; "
            "run boundaries are not causal partitions"
        )
    groups: list[list[str]] = []
    for index, raw_group in enumerate(explicit_raw):
        if not isinstance(raw_group, dict):
            raise ValueError(
                "canonicalize_problem_cases: "
                f"split_groups[{index}] must be an object with evidence_atom_ids"
            )
        group = _clean_relation_string_list(raw_group.get("evidence_atom_ids"))
        if not group:
            raise ValueError(
                f"canonicalize_problem_cases: split_groups[{index}] is empty"
            )
        groups.append(group)

    if len(groups) < 2:
        raise ValueError(
            "canonicalize_problem_cases: split requires at least two explicit evidence groups"
        )
    flattened = [atom_id for group in groups for atom_id in group]
    if len(flattened) != len(set(flattened)):
        raise ValueError("canonicalize_problem_cases: split groups overlap")
    if set(flattened) != set(evidence_ids):
        raise ValueError(
            "canonicalize_problem_cases: split groups must partition all evidence_atom_ids"
        )
    return groups


def canonicalize_problem_cases(
    items: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    *,
    stage: str = "problem_mining",
    strict_review: bool = False,
    verified_mechanism_sha256_by_case: Mapping[str, str] | None = None,
    verified_causal_signature_sha256_by_case: Mapping[str, str] | None = None,
    verified_relation_edges: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Apply reviewer decisions to canonical pre-research problem work units.

    Unlike :func:`apply_relation_decisions`, this API is intentionally operational:
    ``merge``, ``alias``, and ``same_cause_group`` collapse to one canonical case;
    ``split`` creates distinct child cases.  All references are validated strictly so a
    malformed new relation artifact cannot be silently ignored.
    """

    copied = [dict(item) for item in items]
    if not copied:
        if decisions:
            raise ValueError("canonicalize_problem_cases: decisions supplied for no items")
        return []

    by_case: dict[str, dict[str, Any]] = {}
    alias_to_case: dict[str, str] = {}
    order: list[str] = []
    for index, item in enumerate(copied):
        case_id = _clean_relation_string(item.get("case_id"))
        problem_id = _clean_relation_string(item.get("problem_id"))
        if case_id is None or problem_id is None:
            raise ValueError(
                f"canonicalize_problem_cases: items[{index}] requires case_id and problem_id"
            )
        if case_id in by_case:
            raise ValueError(f"canonicalize_problem_cases: duplicate case_id {case_id!r}")
        provisional_group = item.get("provisional_same_cause_group")
        if isinstance(provisional_group, Mapping):
            provisional_errors = provisional_same_cause_group_errors(
                provisional_group,
                owning_case_id=case_id,
            )
            if provisional_errors:
                item["case_identity_status"] = "pending_relation"
                item["provisional_same_cause_integrity_errors"] = provisional_errors
        by_case[case_id] = item
        order.append(case_id)
        for alias in [
            case_id,
            problem_id,
            *_clean_relation_string_list(item.get("case_member_problem_ids")),
        ]:
            previous = alias_to_case.get(alias)
            if previous is not None and previous != case_id:
                raise ValueError(
                    f"canonicalize_problem_cases: ambiguous item reference {alias!r}"
                )
            alias_to_case[alias] = case_id

    parent = {case_id: case_id for case_id in order}

    def find(case_id: str) -> str:
        while parent[case_id] != case_id:
            parent[case_id] = parent[parent[case_id]]
            case_id = parent[case_id]
        return case_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    def resolve(raw_id: Any, *, field: str) -> str:
        identifier = _clean_relation_string(raw_id)
        case_id = alias_to_case.get(identifier or "")
        if case_id is None:
            raise ValueError(
                f"canonicalize_problem_cases: {field} references unknown item {identifier!r}"
            )
        return case_id

    if strict_review:
        decision_by_focus: dict[str, dict[str, Any]] = {}
        peers_by_focus: dict[str, list[str]] = {}

        def collapse_peers(decision: Mapping[str, Any]) -> list[str]:
            action = _clean_relation_string(decision.get("action"))
            if action == "merge":
                return [
                    resolve(target, field="target_ids")
                    for target in _clean_relation_string_list(decision.get("target_ids"))
                ]
            if action == "alias":
                return [resolve(decision.get("alias_target_id"), field="alias_target_id")]
            if action == "same_cause_group":
                focus = resolve(decision.get("focus_id"), field="focus_id")
                return [
                    case_id
                    for case_id in (
                        resolve(member, field="member_ids")
                        for member in _clean_relation_string_list(decision.get("member_ids"))
                    )
                    if case_id != focus
                ]
            return []

        def verified_causal_identity(case_id: str) -> str | None:
            # A normalized mechanism hash identifies a code surface, not necessarily
            # the complete causal path. Only the runner's full causal signature (or a
            # prior verified relation edge) may make a pre-research grouping durable.
            value = _clean_relation_string(
                (verified_causal_signature_sha256_by_case or {}).get(case_id)
            )
            if (
                value is not None
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value.casefold())
            ):
                return value.casefold()
            return None

        def case_owned_identity_evidence(case_id: str) -> set[str]:
            """Return evidence owned by one facet, excluding packet expansion.

            A persisted provisional same-cause group is carried as one research
            packet.  Upstream attachment may therefore place every member's atoms
            on each active member record so the eventual packet is complete.  That
            runner-created overlap is not independent evidence that the case
            identities are already equivalent.  Prefer the immutable member facet
            projection when it is available; genuine shared source evidence still
            overlaps there.
            """

            item = by_case[case_id]
            provisional_group = item.get("provisional_same_cause_group")
            if isinstance(provisional_group, Mapping):
                facets = provisional_group.get("member_facets")
                if isinstance(facets, list):
                    for raw_facet in facets:
                        if not isinstance(raw_facet, Mapping):
                            continue
                        if _clean_relation_string(raw_facet.get("case_id")) != case_id:
                            continue
                        return set(
                            _clean_relation_string_list(
                                raw_facet.get("evidence_atom_ids")
                            )
                        )
            return set(_clean_relation_string_list(item.get("evidence_atom_ids")))

        def objective_identity_edge(left: str, right: str, *, action: str) -> bool:
            left_evidence = case_owned_identity_evidence(left)
            right_evidence = case_owned_identity_evidence(right)
            if left_evidence.intersection(right_evidence):
                return True
            relation_edge = tuple(sorted((left, right)))
            if relation_edge in (verified_relation_edges or set()):
                return True
            left_causal_identity = verified_causal_identity(left)
            right_causal_identity = verified_causal_identity(right)
            return (
                action == "same_cause_group"
                and left_causal_identity is not None
                and left_causal_identity == right_causal_identity
            )

        errors_by_focus: dict[str, list[str]] = {}
        provisional_same_cause_peers: dict[str, set[str]] = {}
        for raw_decision in decisions:
            decision = dict(raw_decision)
            focus_case = resolve(decision.get("focus_id"), field="focus_id")
            if focus_case in decision_by_focus:
                raise ValueError(
                    f"canonicalize_problem_cases: duplicate strict focus {focus_case}"
                )
            decision_by_focus[focus_case] = decision
            errors = errors_by_focus.setdefault(focus_case, [])
            action = _clean_relation_string(decision.get("action")) or ""
            if action not in _VALID_ACTIONS:
                errors.append(f"action_invalid:{action or '(missing)'}")
            if _clean_relation_string(decision.get("rationale")) is None:
                errors.append("rationale_missing")
            confidence = decision.get("review_confidence")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not 0.0 <= float(confidence) <= 1.0
            ):
                errors.append("review_confidence_invalid")
            try:
                peers = collapse_peers(decision) if action in _VALID_ACTIONS else []
            except ValueError as exc:
                peers = []
                errors.append(f"relation_reference_invalid:{exc}")
            peers_by_focus[focus_case] = peers
            if action in {"merge", "alias", "same_cause_group"} and not peers:
                errors.append("collapse_peer_missing")
            if action == "split":
                try:
                    _relation_split_groups(by_case[focus_case], decision)
                except ValueError as exc:
                    errors.append(f"split_partition_invalid:{exc}")
            if action == "same_cause_group" and _clean_relation_string(
                decision.get("group_id")
            ) is None:
                errors.append("same_cause_group_id_missing")
            if action == "keep_separate" and isinstance(
                by_case[focus_case].get("provisional_same_cause_group"), Mapping
            ):
                provisional_group = by_case[focus_case]["provisional_same_cause_group"]
                clearance = {
                    "group_id": provisional_group.get("group_id"),
                    "member_case_ids": _clean_relation_string_list(
                        provisional_group.get("member_case_ids")
                    ),
                    "evidence_atom_ids": _clean_relation_string_list(
                        decision.get("evidence_atom_ids")
                    ),
                }
                clearance_errors = provisional_same_cause_clearance_errors(
                    provisional_group,
                    clearance,
                )
                if clearance_errors:
                    errors.extend(clearance_errors)
                else:
                    decision["_provisional_same_cause_clearance"] = clearance
            if not peers:
                continue
            evidence_refs = set(
                _clean_relation_string_list(decision.get("evidence_atom_ids"))
            )
            focus_evidence = set(
                _clean_relation_string_list(by_case[focus_case].get("evidence_atom_ids"))
            )
            if not evidence_refs.intersection(focus_evidence):
                errors.append("collapse_focus_evidence_missing")
            allowed_evidence = set(focus_evidence)
            for peer_case in peers:
                peer_evidence = set(
                    _clean_relation_string_list(by_case[peer_case].get("evidence_atom_ids"))
                )
                allowed_evidence.update(peer_evidence)
                if not evidence_refs.intersection(peer_evidence):
                    errors.append(f"collapse_peer_evidence_missing:{peer_case}")
                if not objective_identity_edge(focus_case, peer_case, action=action):
                    if action == "same_cause_group":
                        # A reciprocal, evidence-citing same-cause judgment is useful
                        # before the common mechanism can be runner-verified.  Treat it
                        # as a provisional research hypothesis, never as a permanent
                        # alias.  Merge and alias still require an objective identity
                        # edge at this boundary.
                        provisional_same_cause_peers.setdefault(focus_case, set()).add(
                            peer_case
                        )
                        decision["_provisional_same_cause"] = True
                    elif action == "alias":
                        # Direction is not causal evidence. Two facets may independently
                        # alias each other when separate review batches agree they are one
                        # suspected cause but choose opposite owners. Defer that exact
                        # cycle until reciprocal review is available; it can become one
                        # provisional research unit, never a durable alias.
                        errors.append(
                            f"collapse_objective_identity_missing:{peer_case}"
                        )
                    else:
                        errors.append(f"collapse_objective_identity_missing:{peer_case}")
            if not evidence_refs.issubset(allowed_evidence):
                errors.append("collapse_unrelated_evidence_cited")

        active_cases = {
            case_id
            for case_id, item in by_case.items()
            if item.get("_relation_candidate_only") is not True
        }
        if set(decision_by_focus) != active_cases:
            missing = sorted(active_cases - set(decision_by_focus))
            extra = sorted(set(decision_by_focus) - active_cases)
            raise ValueError(
                "canonicalize_problem_cases: strict focus partition mismatch "
                f"missing={missing} extra={extra}"
            )
        for focus_case, decision in decision_by_focus.items():
            for peer_case in peers_by_focus.get(focus_case, []):
                if peer_case not in active_cases:
                    continue
                if focus_case not in peers_by_focus.get(peer_case, []):
                    errors_by_focus[focus_case].append(
                        f"collapse_not_reciprocal:{peer_case}"
                    )
                    errors_by_focus[peer_case].append(
                        f"contradicted_collapse_from:{focus_case}"
                    )
                    continue
                focus_action = _clean_relation_string(decision.get("action"))
                peer_decision = decision_by_focus[peer_case]
                peer_action = _clean_relation_string(peer_decision.get("action"))
                reciprocal_provisional_alias = (
                    focus_action == peer_action == "alias"
                    and peers_by_focus.get(focus_case) == [peer_case]
                    and peers_by_focus.get(peer_case) == [focus_case]
                )
                if reciprocal_provisional_alias:
                    # The first pass deliberately records the normal alias error so a
                    # one-sided or candidate-only alias can never bypass the objective
                    # identity requirement.  Remove only the two exact errors after a
                    # complete reciprocal active-case cycle has been established.
                    for member_case, target_case in (
                        (focus_case, peer_case),
                        (peer_case, focus_case),
                    ):
                        objective_error = (
                            f"collapse_objective_identity_missing:{target_case}"
                        )
                        errors_by_focus[member_case] = [
                            error
                            for error in errors_by_focus[member_case]
                            if error != objective_error
                        ]
                    canonical_group_id = _runner_owned_same_cause_group_id(
                        [focus_case, peer_case],
                        provisional=True,
                    )
                    for alias_decision, member_case, target_case in (
                        (decision, focus_case, peer_case),
                        (peer_decision, peer_case, focus_case),
                    ):
                        alias_decision.setdefault("_submitted_action", "alias")
                        alias_decision.setdefault(
                            "_submitted_alias_target_id",
                            alias_decision.get("alias_target_id"),
                        )
                        alias_decision["action"] = "same_cause_group"
                        alias_decision["group_id"] = canonical_group_id
                        alias_decision["member_ids"] = [member_case, target_case]
                        alias_decision["_provisional_same_cause"] = True
                    provisional_same_cause_peers.setdefault(focus_case, set()).add(
                        peer_case
                    )
                    provisional_same_cause_peers.setdefault(peer_case, set()).add(
                        focus_case
                    )
                    focus_action = peer_action = "same_cause_group"
                compatible = {focus_action, peer_action} <= {"merge", "alias"}
                if focus_action == peer_action == "same_cause_group":
                    focus_members = {focus_case, *peers_by_focus.get(focus_case, [])}
                    peer_members = {peer_case, *peers_by_focus.get(peer_case, [])}
                    provisional_relation = (
                        peer_case
                        in provisional_same_cause_peers.get(focus_case, set())
                        or focus_case
                        in provisional_same_cause_peers.get(peer_case, set())
                    )
                    compatible = focus_members == peer_members
                    if compatible:
                        canonical_group_id = _runner_owned_same_cause_group_id(
                            sorted(focus_members),
                            provisional=provisional_relation,
                        )
                        # Preserve a submitted group label only when the model actually
                        # submitted a group relation. A reciprocal alias converted above
                        # has no model-authored group label and must not manufacture one
                        # in the audit trail.
                        if decision.get("_submitted_action") is None:
                            decision.setdefault(
                                "_submitted_group_id", decision.get("group_id")
                            )
                        if peer_decision.get("_submitted_action") is None:
                            peer_decision.setdefault(
                                "_submitted_group_id", peer_decision.get("group_id")
                            )
                        decision["group_id"] = canonical_group_id
                        peer_decision["group_id"] = canonical_group_id
                if focus_action == peer_action == "alias":
                    compatible = False
                if not compatible:
                    errors_by_focus[focus_case].append(
                        f"collapse_relation_incompatible:{peer_case}"
                    )
                    errors_by_focus[peer_case].append(
                        f"collapse_relation_incompatible:{focus_case}"
                    )
                    continue
                if (
                    focus_action == peer_action == "same_cause_group"
                    and (
                        peer_case
                        in provisional_same_cause_peers.get(focus_case, set())
                        or focus_case
                        in provisional_same_cause_peers.get(peer_case, set())
                    )
                ):
                    # This marker is runner-owned and is applied only after reciprocal
                    # compatibility and evidence coverage have been checked.
                    decision["_provisional_same_cause"] = True
                    peer_decision["_provisional_same_cause"] = True

        strict_decisions: list[dict[str, Any]] = []
        strict_focus_rank = {case_id: index for index, case_id in enumerate(order)}
        for focus_case in sorted(
            decision_by_focus,
            key=lambda case_id: strict_focus_rank[case_id],
        ):
            decision = decision_by_focus[focus_case]
            validation_errors = list(dict.fromkeys(errors_by_focus[focus_case]))
            if not validation_errors:
                strict_decisions.append(decision)
                continue
            strict_decisions.append(
                {
                    "focus_id": decision.get("focus_id"),
                    "action": "keep_separate",
                    "rationale": (
                        "Runner kept this case independent because the proposed relation "
                        "lacked an objective identity edge: " + ", ".join(validation_errors)
                    ),
                    "review_confidence": 0.0,
                    "provisional_relation_suggestion": decision,
                    "relation_validation_errors": validation_errors,
                }
            )
        decisions = strict_decisions

    preferences: list[str] = []
    split_by_case: dict[str, dict[str, Any]] = {}
    group_by_case: dict[str, str] = {}
    provisional_group_ids: set[str] = set()
    provisional_clearance_by_case: dict[str, dict[str, Any]] = {}
    audit_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in order}

    for index, raw_decision in enumerate(decisions):
        decision = dict(raw_decision)
        action = _clean_relation_string(decision.get("action"))
        if action not in _VALID_ACTIONS:
            raise ValueError(
                f"canonicalize_problem_cases: decisions[{index}] invalid action {action!r}"
            )
        focus_case = resolve(decision.get("focus_id"), field="focus_id")
        audit_entry = {
            "action": action,
            "rationale": _clean_relation_string(decision.get("rationale")) or "",
            "review_confidence": decision.get("review_confidence"),
        }
        submitted_group_id = _clean_relation_string(decision.get("_submitted_group_id"))
        if submitted_group_id is not None:
            audit_entry["submitted_group_id"] = submitted_group_id
            audit_entry["canonical_group_id"] = _clean_relation_string(
                decision.get("group_id")
            )
        submitted_action = _clean_relation_string(decision.get("_submitted_action"))
        if submitted_action is not None:
            audit_entry["submitted_action"] = submitted_action
            audit_entry["submitted_alias_target_id"] = _clean_relation_string(
                decision.get("_submitted_alias_target_id")
            )
            audit_entry["canonical_group_id"] = _clean_relation_string(
                decision.get("group_id")
            )
        if isinstance(decision.get("provisional_relation_suggestion"), Mapping):
            audit_entry["provisional_relation_suggestion"] = dict(
                decision["provisional_relation_suggestion"]
            )
            audit_entry["relation_validation_errors"] = _clean_relation_string_list(
                decision.get("relation_validation_errors")
            )

        if action == "keep_separate":
            clearance = decision.get("_provisional_same_cause_clearance")
            if isinstance(clearance, Mapping):
                provisional_clearance_by_case[focus_case] = dict(clearance)
            audit_by_case[focus_case].append(audit_entry)
            continue
        if action == "split":
            if focus_case in split_by_case:
                raise ValueError(
                    f"canonicalize_problem_cases: duplicate split for {focus_case}"
                )
            split_by_case[focus_case] = decision
            audit_by_case[focus_case].append(audit_entry)
            continue
        if action == "merge":
            targets = _clean_relation_string_list(decision.get("target_ids"))
            if not targets:
                raise ValueError("canonicalize_problem_cases: merge requires target_ids")
            preferences.append(focus_case)
            for target in targets:
                target_case = resolve(target, field="target_ids")
                union(focus_case, target_case)
                audit_by_case[focus_case].append({**audit_entry, "target_case_id": target_case})
            continue
        if action == "alias":
            target_case = resolve(decision.get("alias_target_id"), field="alias_target_id")
            preferences.append(target_case)
            union(target_case, focus_case)
            audit_by_case[focus_case].append({**audit_entry, "target_case_id": target_case})
            continue

        group_id = _clean_relation_string(decision.get("group_id"))
        if group_id is None:
            raise ValueError("canonicalize_problem_cases: same_cause_group requires group_id")
        members = [focus_case]
        members.extend(
            resolve(member, field="member_ids")
            for member in _clean_relation_string_list(decision.get("member_ids"))
        )
        members = list(dict.fromkeys(members))
        if len(members) < 2:
            raise ValueError(
                "canonicalize_problem_cases: same_cause_group requires at least two members"
            )
        preferences.append(focus_case)
        if decision.get("_provisional_same_cause") is True:
            provisional_group_ids.add(group_id)
        for member in members:
            previous_group = group_by_case.get(member)
            if previous_group is not None and previous_group != group_id:
                raise ValueError(
                    f"canonicalize_problem_cases: case {member} assigned conflicting groups"
                )
            group_by_case[member] = group_id
            union(focus_case, member)
            audit_by_case[member].append({**audit_entry, "group_id": group_id})

    components: dict[str, list[str]] = {}
    for case_id in order:
        components.setdefault(find(case_id), []).append(case_id)

    preference_rank = {case_id: rank for rank, case_id in enumerate(preferences)}
    order_rank = {case_id: rank for rank, case_id in enumerate(order)}
    canonical_records: list[dict[str, Any]] = []
    for members in sorted(components.values(), key=lambda value: min(order_rank[v] for v in value)):
        split_members = [case_id for case_id in members if case_id in split_by_case]
        if split_members and len(members) > 1:
            raise ValueError(
                "canonicalize_problem_cases: split cannot be combined with merge/alias/"
                "same_cause_group in one component"
            )
        component_has_same_cause_group = any(
            group_by_case.get(case_id) is not None for case_id in members
        )
        if strict_review and component_has_same_cause_group:
            # Model response ordering is not identity evidence.  A strict same-cause
            # work unit keeps a persisted candidate when one exists, otherwise the
            # stable case ID determines its representative across response permutations.
            preferred = min(
                members,
                key=lambda case_id: (
                    0
                    if by_case[case_id].get("_relation_candidate_only") is True
                    else 1,
                    case_id,
                ),
            )
        else:
            preferred = min(
                members,
                key=lambda case_id: (
                    # A candidate-only record came from the persisted case registry.
                    # Its durable identity must survive a recurrence even when the
                    # reviewer uses merge/same_cause_group instead of the preferred
                    # alias spelling.
                    0
                    if by_case[case_id].get("_relation_candidate_only") is True
                    else 1,
                    preference_rank.get(case_id, 10**9),
                    order_rank[case_id],
                ),
            )
        base = dict(by_case[preferred])
        provisional_clearance = provisional_clearance_by_case.get(preferred)
        if provisional_clearance is not None:
            base.pop("provisional_same_cause_group", None)
            base.pop("case_identity_candidate_ids", None)
            base.pop("provisional_same_cause_integrity_errors", None)
            base["case_identity_status"] = "resolved"
            base["provisional_same_cause_clearance"] = provisional_clearance

        if split_members:
            split_case = split_members[0]
            split_item = by_case[split_case]
            groups = _relation_split_groups(split_item, split_by_case[split_case])
            parent_problem_id = _clean_relation_string(split_item.get("problem_id")) or "problem"
            parent_problem_ids = list(
                dict.fromkeys(
                    [parent_problem_id]
                    + _clean_relation_string_list(
                        split_item.get("case_member_problem_ids")
                    )
                )
            )
            for split_index, evidence_group in enumerate(groups, start=1):
                child = dict(split_item)
                child_case_id = mint_case_id(
                    [split_case, *evidence_group], namespace="case_split"
                )
                child["case_id"] = child_case_id
                child["problem_id"] = f"{parent_problem_id}:split:{split_index}"
                child["canonical_problem_id"] = child["problem_id"]
                child["case_member_problem_ids"] = [child["problem_id"]]
                child["evidence_atom_ids"] = evidence_group
                parent_source_ids = set(
                    _clean_relation_string_list(
                        split_item.get("source_evidence_atom_ids")
                    )
                )
                parent_derived_ids = set(
                    _clean_relation_string_list(
                        split_item.get("derived_evidence_atom_ids")
                    )
                )
                child["source_evidence_atom_ids"] = [
                    atom_id for atom_id in evidence_group if atom_id in parent_source_ids
                ]
                child["derived_evidence_atom_ids"] = [
                    atom_id for atom_id in evidence_group if atom_id in parent_derived_ids
                ]
                child["split_from_case_id"] = split_case
                child["split_parent_problem_id"] = parent_problem_id
                child["split_parent_problem_ids"] = parent_problem_ids
                child["related_case_ids"] = list(
                    dict.fromkeys(
                        _clean_relation_string_list(child.get("related_case_ids"))
                        + [split_case]
                    )
                )
                child["case_relation_actions"] = audit_by_case[split_case]
                canonical_records.append(child)
            continue

        evidence_ids: list[str] = []
        source_evidence_ids: list[str] = []
        derived_evidence_ids: list[str] = []
        problem_ids: list[str] = []
        relation_actions: list[dict[str, Any]] = []
        group_ids: set[str] = set()
        for member in members:
            item = by_case[member]
            evidence_ids.extend(_clean_relation_string_list(item.get("evidence_atom_ids")))
            source_evidence_ids.extend(
                _clean_relation_string_list(item.get("source_evidence_atom_ids"))
            )
            derived_evidence_ids.extend(
                _clean_relation_string_list(item.get("derived_evidence_atom_ids"))
            )
            problem_id = _clean_relation_string(item.get("problem_id"))
            if problem_id is not None:
                problem_ids.append(problem_id)
            problem_ids.extend(_clean_relation_string_list(item.get("case_member_problem_ids")))
            relation_actions.extend(audit_by_case[member])
            group_id = group_by_case.get(member) or _clean_relation_string(
                item.get("same_cause_group_id")
            )
            if group_id is not None:
                group_ids.add(group_id)
        if len(group_ids) > 1:
            raise ValueError(
                f"canonicalize_problem_cases: component has conflicting group IDs {group_ids}"
            )

        canonical_problem_id = _clean_relation_string(base.get("problem_id"))
        assert canonical_problem_id is not None
        base["canonical_problem_id"] = canonical_problem_id
        base["case_member_problem_ids"] = list(dict.fromkeys(problem_ids))
        base["evidence_atom_ids"] = list(dict.fromkeys(evidence_ids))
        base["source_evidence_atom_ids"] = list(dict.fromkeys(source_evidence_ids))
        base["derived_evidence_atom_ids"] = list(dict.fromkeys(derived_evidence_ids))
        absorbed = [case_id for case_id in members if case_id != preferred]
        if relation_actions:
            base["case_relation_actions"] = relation_actions
        component_group_id = next(iter(group_ids)) if group_ids else None
        provisional_group = (
            component_group_id is not None
            and component_group_id in provisional_group_ids
        )
        if provisional_group:
            member_facets = []
            for member in members:
                item = by_case[member]
                member_facets.append(
                    {
                        "case_id": member,
                        "problem_id": _clean_relation_string(item.get("problem_id")),
                        "title": _clean_relation_string(item.get("title")),
                        "problem": _clean_relation_string(item.get("problem")),
                        "user_impact": _clean_relation_string(item.get("user_impact")),
                        "canonical_symptoms": _clean_relation_string_list(
                            item.get("canonical_symptoms")
                        ),
                        "evidence_atom_ids": _clean_relation_string_list(
                            item.get("evidence_atom_ids")
                        ),
                        "source_evidence_atom_ids": _clean_relation_string_list(
                            item.get("source_evidence_atom_ids")
                        ),
                    }
                )
            base.pop("absorbed_case_ids", None)
            base.pop("same_cause_group_id", None)
            base["case_identity_status"] = "provisional_same_cause"
            base["case_identity_candidate_ids"] = list(members)
            base["provisional_same_cause_group"] = {
                "schema_version": 1,
                "status": "research_hypothesis",
                "group_id": component_group_id,
                "member_case_ids": list(members),
                "member_problem_ids": list(dict.fromkeys(problem_ids)),
                "member_facets": member_facets,
            }
        else:
            if absorbed:
                base["absorbed_case_ids"] = absorbed
            if component_group_id is not None:
                base["same_cause_group_id"] = component_group_id
            pending_candidates = set(
                _clean_relation_string_list(base.get("case_identity_candidate_ids"))
            )
            if (
                base.get("case_identity_status") == "pending_relation"
                and pending_candidates
                and pending_candidates.issubset(set(members))
                and len(members) > 1
            ):
                base["case_identity_status"] = "resolved"
                base.pop("case_identity_candidate_ids", None)
        canonical_records.append(base)

    _LOG.info(
        "canonicalize_problem_cases: stage=%s input=%d decisions=%d output=%d",
        stage,
        len(copied),
        len(decisions),
        len(canonical_records),
    )
    return canonical_records
