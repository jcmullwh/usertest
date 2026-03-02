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
from collections.abc import Sequence
from typing import Any, Protocol

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
    n = len(items)

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
    from triage_engine.text import tokenize

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
            for tid in target_ids:
                target_item = by_focus_id.get(tid)
                if target_item is None:
                    _LOG.warning(
                        "apply_relation_decisions: merge target_id=%s not found; skipping",
                        tid,
                    )
                    continue
                merged_evidence.extend(target_item.get("evidence_atom_ids") or [])
                removed_ids.add(tid)
            # Deduplicate evidence IDs while preserving order.
            seen: set[str] = set()
            deduped: list[str] = []
            for eid in merged_evidence:
                if eid not in seen:
                    seen.add(eid)
                    deduped.append(eid)
            focus_item["evidence_atom_ids"] = deduped
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
                item = by_focus_id.get(iid)
                if item is None:
                    _LOG.warning(
                        "apply_relation_decisions: same_cause_group id=%s not found; skipping",
                        iid,
                    )
                    continue
                item["same_cause_group_id"] = group_id
                by_focus_id[iid] = item
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
