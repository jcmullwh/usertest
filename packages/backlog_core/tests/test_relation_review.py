"""Tests for backlog_core.relation_review.

Key invariants asserted here:
- rank_stage_related_items raises for unknown stages.
- rank_stage_related_items returns one neighborhood per item.
- Each neighborhood exposes per-signal lists separately.
- apply_relation_decisions applies merge, split, same_cause_group, alias, keep_separate.
- apply_relation_decisions never makes implicit decisions; decisions must be explicit.
- Automatic clustering alone does NOT decide the final grouping.
"""

from __future__ import annotations

import pytest

from backlog_core.relation_review import apply_relation_decisions, rank_stage_related_items

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_RELATION_CONFIG: dict = {
    "version": 1,
    "defaults": {
        "top_k_by_semantic": 3,
        "top_k_by_evidence_overlap": 3,
        "top_k_by_metadata": 2,
        "top_k_by_path_anchor": 2,
        "union_cap": 8,
        "min_evidence_overlap": 1,
        "min_semantic_similarity": 0.55,
        "split_hint_min_disjoint_evidence_fraction": 0.70,
    },
    "stages": {},
}


def _make_problem_record(
    pid: str,
    evidence_atom_ids: list[str] | None = None,
    title: str = "",
) -> dict:
    return {
        "problem_id": pid,
        "title": title or pid,
        "problem": f"problem text for {pid}",
        "user_impact": "users affected",
        "severity": "high",
        "confidence": 0.8,
        "evidence_atom_ids": evidence_atom_ids or [f"run/20260101/{pid}:cp:1"],
        "evidence_summary": "test evidence",
        "problem_status": "identified",
    }


# ---------------------------------------------------------------------------
# rank_stage_related_items
# ---------------------------------------------------------------------------


def test_rank_raises_on_unknown_stage() -> None:
    items = [_make_problem_record("problem:a")]
    with pytest.raises(ValueError, match="unknown stage"):
        rank_stage_related_items(
            items,
            stage="nonexistent_stage",
            relation_config=_MINIMAL_RELATION_CONFIG,
        )


def test_rank_returns_empty_for_empty_input() -> None:
    result = rank_stage_related_items(
        [],
        stage="problem_mining",
        relation_config=_MINIMAL_RELATION_CONFIG,
    )
    assert result == []


def test_rank_returns_one_neighborhood_per_item() -> None:
    items = [
        _make_problem_record("problem:a"),
        _make_problem_record("problem:b"),
        _make_problem_record("problem:c"),
    ]
    result = rank_stage_related_items(
        items,
        stage="problem_mining",
        relation_config=_MINIMAL_RELATION_CONFIG,
    )
    assert len(result) == len(items)


def test_rank_neighborhood_has_required_keys() -> None:
    items = [_make_problem_record("problem:a"), _make_problem_record("problem:b")]
    neighborhoods = rank_stage_related_items(
        items,
        stage="problem_mining",
        relation_config=_MINIMAL_RELATION_CONFIG,
    )
    required_keys = {
        "focus_id",
        "stage",
        "most_related_by_semantic",
        "most_related_by_evidence_overlap",
        "most_related_by_metadata",
        "most_related_by_path_anchor",
        "split_hints",
    }
    for nb in neighborhoods:
        assert required_keys.issubset(nb.keys()), (
            f"Missing keys: {required_keys - set(nb.keys())}"
        )


def test_rank_neighborhood_signals_are_separate_lists() -> None:
    """Per-signal lists must be separate (not merged into one 'related' list)."""
    items = [
        _make_problem_record(
            "problem:a",
            evidence_atom_ids=["run/20260101/codex/0:cp:1"],
        ),
        _make_problem_record(
            "problem:b",
            evidence_atom_ids=["run/20260101/codex/0:cp:1"],  # shared evidence
        ),
    ]
    neighborhoods = rank_stage_related_items(
        items,
        stage="problem_mining",
        relation_config=_MINIMAL_RELATION_CONFIG,
    )
    for nb in neighborhoods:
        # Each signal list must be a list (even if empty).
        assert isinstance(nb["most_related_by_semantic"], list)
        assert isinstance(nb["most_related_by_evidence_overlap"], list)
        assert isinstance(nb["most_related_by_metadata"], list)
        assert isinstance(nb["most_related_by_path_anchor"], list)


def test_rank_shared_evidence_produces_evidence_overlap_neighbor() -> None:
    """Items sharing an evidence atom should appear in each other's evidence_overlap list."""
    shared_eid = "run/20260101/codex/0:confusion_point:1"
    items = [
        _make_problem_record("problem:a", evidence_atom_ids=[shared_eid]),
        _make_problem_record("problem:b", evidence_atom_ids=[shared_eid]),
    ]
    neighborhoods = rank_stage_related_items(
        items,
        stage="problem_mining",
        relation_config=_MINIMAL_RELATION_CONFIG,
    )
    # Both neighborhoods should show the other as an evidence_overlap neighbor.
    nb_a = next(nb for nb in neighborhoods if nb["focus_id"] == "problem:a")
    nb_b = next(nb for nb in neighborhoods if nb["focus_id"] == "problem:b")
    assert any(n["item_id"] == "problem:b" for n in nb_a["most_related_by_evidence_overlap"])
    assert any(n["item_id"] == "problem:a" for n in nb_b["most_related_by_evidence_overlap"])


def test_rank_automatic_logic_does_not_decide_grouping() -> None:
    """Neighborhoods are candidates only.  No merges should occur automatically."""
    items = [
        _make_problem_record("problem:a", evidence_atom_ids=["run/ts/codex/0:cp:1"]),
        _make_problem_record("problem:b", evidence_atom_ids=["run/ts/codex/0:cp:1"]),
    ]
    # Even with shared evidence, rank_stage_related_items does not merge items.
    result = rank_stage_related_items(
        items,
        stage="problem_mining",
        relation_config=_MINIMAL_RELATION_CONFIG,
    )
    assert len(result) == 2  # Both items still present; no merging happened.


def test_rank_split_hint_on_disjoint_evidence() -> None:
    """An item with evidence from very different runs should get a split hint."""
    # Two completely distinct run prefixes.
    eids = [
        "target_a/20260101T000000Z/codex/0:confusion_point:1",
        "target_a/20260101T000000Z/codex/0:confusion_point:2",
        "target_b/20260201T000000Z/claude/0:confusion_point:1",
        "target_b/20260201T000000Z/claude/0:confusion_point:2",
    ]
    items = [_make_problem_record("problem:split-candidate", evidence_atom_ids=eids)]
    # Use a low threshold to ensure the hint fires.
    config = {
        "version": 1,
        "defaults": {
            "split_hint_min_disjoint_evidence_fraction": 0.1,
        },
        "stages": {},
    }
    neighborhoods = rank_stage_related_items(
        items,
        stage="problem_mining",
        relation_config=config,
    )
    nb = neighborhoods[0]
    assert len(nb["split_hints"]) > 0
    assert nb["split_hints"][0]["hint_type"] == "disjoint_evidence_groups"


def test_rank_all_valid_stages_accepted() -> None:
    items = [_make_problem_record("problem:a")]
    for stage in (
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
    ):
        result = rank_stage_related_items(
            items, stage=stage, relation_config=_MINIMAL_RELATION_CONFIG
        )
        assert len(result) == 1


# ---------------------------------------------------------------------------
# apply_relation_decisions
# ---------------------------------------------------------------------------


def test_apply_keep_separate_leaves_items_unchanged() -> None:
    items = [
        _make_problem_record("problem:a"),
        _make_problem_record("problem:b"),
    ]
    decisions = [
        {"action": "keep_separate", "focus_id": "problem:a", "rationale": "distinct"},
    ]
    result = apply_relation_decisions(items, decisions, stage="problem_mining")
    assert len(result) == 2
    assert result[0]["problem_id"] == "problem:a"
    assert result[1]["problem_id"] == "problem:b"


def test_apply_merge_absorbs_target_evidence() -> None:
    items = [
        _make_problem_record("problem:a", evidence_atom_ids=["run/ts/x:cp:1"]),
        _make_problem_record("problem:b", evidence_atom_ids=["run/ts/y:cp:1"]),
    ]
    decisions = [
        {
            "action": "merge",
            "focus_id": "problem:a",
            "target_ids": ["problem:b"],
            "rationale": "Same root cause",
        }
    ]
    result = apply_relation_decisions(items, decisions, stage="problem_mining")
    # After merge, problem:b is removed.
    assert len(result) == 1
    assert result[0]["problem_id"] == "problem:a"
    # Evidence from b absorbed into a.
    eids = result[0]["evidence_atom_ids"]
    assert "run/ts/x:cp:1" in eids
    assert "run/ts/y:cp:1" in eids
    assert result[0]["_merged_from"] == ["problem:b"]


def test_apply_merge_deduplicates_evidence() -> None:
    shared = "run/ts/shared:cp:1"
    items = [
        _make_problem_record("problem:a", evidence_atom_ids=[shared, "run/ts/a:cp:1"]),
        _make_problem_record("problem:b", evidence_atom_ids=[shared, "run/ts/b:cp:1"]),
    ]
    decisions = [
        {
            "action": "merge",
            "focus_id": "problem:a",
            "target_ids": ["problem:b"],
            "rationale": "",
        }
    ]
    result = apply_relation_decisions(items, decisions, stage="problem_mining")
    assert result[0]["evidence_atom_ids"].count(shared) == 1


def test_apply_split_annotates_item() -> None:
    items = [_make_problem_record("problem:a")]
    decisions = [
        {
            "action": "split",
            "focus_id": "problem:a",
            "rationale": "Two distinct components",
        }
    ]
    result = apply_relation_decisions(items, decisions, stage="problem_mining")
    assert len(result) == 1
    assert result[0]["_split_hint_acknowledged"] is True
    assert result[0]["_split_rationale"] == "Two distinct components"


def test_apply_same_cause_group_adds_group_id() -> None:
    items = [
        _make_problem_record("problem:a"),
        _make_problem_record("problem:b"),
    ]
    decisions = [
        {
            "action": "same_cause_group",
            "focus_id": "problem:a",
            "group_id": "group:root-cause-x",
            "member_ids": ["problem:b"],
            "rationale": "Same root",
        }
    ]
    result = apply_relation_decisions(items, decisions, stage="problem_mining")
    assert len(result) == 2
    for item in result:
        assert item.get("same_cause_group_id") == "group:root-cause-x"


def test_apply_alias_annotates_item() -> None:
    items = [
        _make_problem_record("problem:a"),
        _make_problem_record("problem:b"),
    ]
    decisions = [
        {
            "action": "alias",
            "focus_id": "problem:a",
            "alias_target_id": "problem:b",
            "rationale": "Duplicate title",
        }
    ]
    result = apply_relation_decisions(items, decisions, stage="problem_mining")
    assert len(result) == 2
    focus = next(r for r in result if r["problem_id"] == "problem:a")
    assert focus["_alias_of"] == "problem:b"


def test_apply_raises_on_unknown_action() -> None:
    items = [_make_problem_record("problem:a")]
    decisions = [{"action": "auto_merge", "focus_id": "problem:a"}]
    with pytest.raises(ValueError, match="unknown action"):
        apply_relation_decisions(items, decisions, stage="problem_mining")


def test_apply_empty_decisions_returns_original() -> None:
    items = [_make_problem_record("problem:a")]
    result = apply_relation_decisions(items, [], stage="problem_mining")
    assert len(result) == 1
    assert result[0]["problem_id"] == "problem:a"


def test_apply_missing_focus_id_skips_gracefully() -> None:
    """A decision referencing a nonexistent focus_id should be skipped with a warning."""
    items = [_make_problem_record("problem:a")]
    decisions = [
        {
            "action": "merge",
            "focus_id": "problem:nonexistent",
            "target_ids": ["problem:a"],
            "rationale": "...",
        }
    ]
    # Should not raise; the decision is skipped.
    result = apply_relation_decisions(items, decisions, stage="problem_mining")
    assert len(result) == 1
