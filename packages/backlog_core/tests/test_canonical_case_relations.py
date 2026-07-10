from __future__ import annotations

import pytest

from backlog_core.relation_review import canonicalize_problem_cases


def _case(
    problem_id: str,
    case_id: str,
    evidence_atom_ids: list[str],
) -> dict[str, object]:
    return {
        "problem_id": problem_id,
        "canonical_problem_id": problem_id,
        "case_id": case_id,
        "case_member_problem_ids": [problem_id],
        "evidence_atom_ids": evidence_atom_ids,
        "source_evidence_atom_ids": evidence_atom_ids,
        "derived_evidence_atom_ids": [],
        "title": problem_id,
    }


def test_same_cause_group_is_one_operational_work_unit() -> None:
    items = [
        _case("problem:a", "case:a", ["target/run1/agent/1:cp:1"]),
        _case("problem:b", "case:b", ["target/run2/agent/1:cp:1"]),
    ]
    result = canonicalize_problem_cases(
        items,
        [
            {
                "focus_id": "problem:a",
                "action": "same_cause_group",
                "group_id": "cause:shared",
                "member_ids": ["problem:b"],
                "rationale": "One mechanism",
            }
        ],
    )

    assert len(result) == 1
    assert result[0]["case_id"] == "case:a"
    assert result[0]["same_cause_group_id"] == "cause:shared"
    assert result[0]["case_member_problem_ids"] == ["problem:a", "problem:b"]
    assert result[0]["absorbed_case_ids"] == ["case:b"]


def test_alias_uses_target_as_canonical_case() -> None:
    items = [
        _case("problem:a", "case:a", ["target/run1/agent/1:cp:1"]),
        _case("problem:b", "case:b", ["target/run2/agent/1:cp:1"]),
    ]
    result = canonicalize_problem_cases(
        items,
        [
            {
                "focus_id": "problem:a",
                "action": "alias",
                "alias_target_id": "problem:b",
                "rationale": "A aliases B",
            }
        ],
    )

    assert len(result) == 1
    assert result[0]["case_id"] == "case:b"
    assert set(result[0]["evidence_atom_ids"]) == {
        "target/run1/agent/1:cp:1",
        "target/run2/agent/1:cp:1",
    }
    assert result[0]["source_evidence_atom_ids"] == result[0]["evidence_atom_ids"]
    assert result[0]["derived_evidence_atom_ids"] == []


def test_split_creates_distinct_child_cases() -> None:
    items = [
        _case(
            "problem:a",
            "case:a",
            ["target/run1/agent/1:cp:1", "target/run2/agent/1:cp:1"],
        )
    ]
    result = canonicalize_problem_cases(
        items,
        [
            {
                "focus_id": "problem:a",
                "action": "split",
                "rationale": "Independent run groups",
                "split_groups": [
                    {"evidence_atom_ids": ["target/run1/agent/1:cp:1"]},
                    {"evidence_atom_ids": ["target/run2/agent/1:cp:1"]},
                ],
            }
        ],
    )

    assert len(result) == 2
    assert {item["split_from_case_id"] for item in result} == {"case:a"}
    assert len({item["case_id"] for item in result}) == 2
    assert {tuple(item["evidence_atom_ids"]) for item in result} == {
        ("target/run1/agent/1:cp:1",),
        ("target/run2/agent/1:cp:1",),
    }
    assert {tuple(item["source_evidence_atom_ids"]) for item in result} == {
        ("target/run1/agent/1:cp:1",),
        ("target/run2/agent/1:cp:1",),
    }
    assert {item["split_parent_problem_id"] for item in result} == {"problem:a"}


def test_split_without_explicit_partition_fails_closed() -> None:
    with pytest.raises(ValueError, match="requires explicit split_groups"):
        canonicalize_problem_cases(
            [
                _case(
                    "problem:a",
                    "case:a",
                    ["target/run1/agent/1:cp:1", "target/run2/agent/1:cp:1"],
                )
            ],
            [{"focus_id": "problem:a", "action": "split", "rationale": "Two causes"}],
        )


def test_malformed_new_relation_reference_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown item"):
        canonicalize_problem_cases(
            [_case("problem:a", "case:a", ["target/run1/agent/1:cp:1"])],
            [
                {
                    "focus_id": "problem:a",
                    "action": "merge",
                    "target_ids": ["problem:missing"],
                }
            ],
        )


def test_strict_unknown_relation_target_is_provisional_not_cycle_fatal() -> None:
    result = canonicalize_problem_cases(
        [_case("problem:a", "case:a", ["atom:a"])],
        [
            {
                "focus_id": "problem:a",
                "action": "alias",
                "alias_target_id": "problem:model-invented",
                "evidence_atom_ids": ["atom:a"],
                "rationale": "The model guessed a historical alias.",
                "review_confidence": 0.9,
            }
        ],
        strict_review=True,
    )

    assert len(result) == 1
    action = result[0]["case_relation_actions"][0]
    assert action["action"] == "keep_separate"
    assert any(
        error.startswith("relation_reference_invalid:")
        for error in action["relation_validation_errors"]
    )


def test_strict_relation_rejects_one_sided_contradictory_merge() -> None:
    items = [
        _case("problem:a", "case:a", ["atom:a"]),
        _case("problem:b", "case:b", ["atom:b"]),
    ]
    result = canonicalize_problem_cases(
        items,
        [
            {
                "focus_id": "problem:a",
                "action": "merge",
                "target_ids": ["problem:b"],
                "evidence_atom_ids": ["atom:a", "atom:b"],
                "rationale": "The cited observations appear to share a cause.",
                "review_confidence": 0.9,
            },
            {
                "focus_id": "problem:b",
                "action": "keep_separate",
                "rationale": "The observed failure surfaces differ.",
                "review_confidence": 0.9,
            },
        ],
        strict_review=True,
    )

    assert len(result) == 2
    assert any(
        "collapse_not_reciprocal:case:b" in action.get("relation_validation_errors", [])
        for action in result[0]["case_relation_actions"]
    )


@pytest.mark.parametrize(
    ("rationale", "confidence", "expected"),
    [("", 0.9, "requires rationale"), ("Grounded", "high", "review_confidence")],
)
def test_strict_relation_requires_rationale_and_numeric_confidence(
    rationale: str,
    confidence: object,
    expected: str,
) -> None:
    result = canonicalize_problem_cases(
        [_case("problem:a", "case:a", ["atom:a"])],
        [
            {
                "focus_id": "problem:a",
                "action": "keep_separate",
                "rationale": rationale,
                "review_confidence": confidence,
            }
        ],
        strict_review=True,
    )

    assert len(result) == 1
    errors = result[0]["case_relation_actions"][0]["relation_validation_errors"]
    expected_error = (
        "rationale_missing" if "rationale" in expected else "review_confidence_invalid"
    )
    assert expected_error in errors


def test_strict_relation_rejects_unevidenced_disjoint_collapse() -> None:
    current = _case("problem:current", "case:current", ["atom:current"])
    historical = _case("problem:historical", "case:historical", ["atom:historical"])
    historical["_relation_candidate_only"] = True
    result = canonicalize_problem_cases(
        [current, historical],
        [
            {
                "focus_id": "problem:current",
                "action": "alias",
                "alias_target_id": "problem:historical",
                "evidence_atom_ids": ["atom:current"],
                "rationale": "The titles look similar.",
                "review_confidence": 0.9,
            }
        ],
        strict_review=True,
    )

    assert len(result) == 2
    errors = result[0]["case_relation_actions"][0]["relation_validation_errors"]
    assert "collapse_peer_evidence_missing:case:historical" in errors
    assert "collapse_objective_identity_missing:case:historical" in errors


def test_model_supplied_prior_relation_cannot_alias_disjoint_cases() -> None:
    current = _case("problem:current", "case:current", ["atom:current"])
    historical = _case("problem:historical", "case:historical", ["atom:historical"])
    historical["_relation_candidate_only"] = True
    current["alias_of"] = "case:historical"
    current["relation_receipt"] = {
        "source_case_id": "case:current",
        "target_case_id": "case:historical",
    }
    decision = {
        "focus_id": "problem:current",
        "action": "alias",
        "alias_target_id": "problem:historical",
        "evidence_atom_ids": ["atom:current", "atom:historical"],
        "rationale": "The model supplied what looks like an old relation.",
        "review_confidence": 0.95,
    }

    untrusted = canonicalize_problem_cases(
        [current, historical],
        [decision],
        strict_review=True,
    )
    trusted = canonicalize_problem_cases(
        [current, historical],
        [decision],
        strict_review=True,
        verified_relation_edges={("case:current", "case:historical")},
    )

    assert len(untrusted) == 2
    assert "collapse_objective_identity_missing:case:historical" in untrusted[0][
        "case_relation_actions"
    ][0]["relation_validation_errors"]
    assert len(trusted) == 1
    assert trusted[0]["case_id"] == "case:historical"


def test_strict_relation_allows_exact_shared_source_identity() -> None:
    items = [
        _case("problem:a", "case:a", ["atom:shared"]),
        _case("problem:b", "case:b", ["atom:shared"]),
    ]
    result = canonicalize_problem_cases(
        items,
        [
            {
                "focus_id": "problem:a",
                "action": "merge",
                "target_ids": ["problem:b"],
                "evidence_atom_ids": ["atom:shared"],
                "rationale": "Both generated records cite the exact same source observation.",
                "review_confidence": 0.95,
            },
            {
                "focus_id": "problem:b",
                "action": "merge",
                "target_ids": ["problem:a"],
                "evidence_atom_ids": ["atom:shared"],
                "rationale": "Both generated records cite the exact same source observation.",
                "review_confidence": 0.95,
            },
        ],
        strict_review=True,
    )

    assert len(result) == 1


def _reciprocal_same_cause_decisions() -> list[dict[str, object]]:
    return [
        {
            "focus_id": "problem:a",
            "action": "same_cause_group",
            "group_id": "cause:verified",
            "member_ids": ["problem:a", "problem:b"],
            "evidence_atom_ids": ["atom:a", "atom:b"],
            "rationale": "The runner verified the same mechanism.",
            "review_confidence": 0.95,
        },
        {
            "focus_id": "problem:b",
            "action": "same_cause_group",
            "group_id": "cause:verified",
            "member_ids": ["problem:a", "problem:b"],
            "evidence_atom_ids": ["atom:a", "atom:b"],
            "rationale": "The runner verified the same mechanism.",
            "review_confidence": 0.95,
        },
    ]


def test_model_supplied_mechanism_hash_cannot_collapse_disjoint_cases() -> None:
    items = [
        _case("problem:a", "case:a", ["atom:a"]),
        _case("problem:b", "case:b", ["atom:b"]),
    ]
    for item in items:
        item["root_cause_status"] = "established"
        item["verified_mechanism_sha256"] = "a" * 64

    result = canonicalize_problem_cases(
        items,
        _reciprocal_same_cause_decisions(),
        strict_review=True,
    )

    assert len(result) == 2
    assert all(
        any(
            error.startswith("collapse_objective_identity_missing:")
            for error in item["case_relation_actions"][0]["relation_validation_errors"]
        )
        for item in result
    )


def test_runner_verified_mechanism_identity_allows_post_research_grouping() -> None:
    items = [
        _case("problem:a", "case:a", ["atom:a"]),
        _case("problem:b", "case:b", ["atom:b"]),
    ]

    result = canonicalize_problem_cases(
        items,
        _reciprocal_same_cause_decisions(),
        strict_review=True,
        verified_mechanism_sha256_by_case={
            "case:a": "a" * 64,
            "case:b": "a" * 64,
        },
    )

    assert len(result) == 1
    assert result[0]["same_cause_group_id"] == "cause:verified"


@pytest.mark.parametrize("action", ["merge", "same_cause_group"])
def test_historical_case_identity_survives_current_recurrence(action: str) -> None:
    current = _case("problem:current", "case:current", ["atom:current"])
    historical = _case("problem:historical", "case:persisted", ["atom:historical"])
    historical["_relation_candidate_only"] = True
    decision: dict[str, object] = {
        "focus_id": "problem:current",
        "action": action,
        "rationale": "The current evidence is the same persisted case.",
    }
    if action == "merge":
        decision["target_ids"] = ["problem:historical"]
    else:
        decision["group_id"] = "cause:persisted"
        decision["member_ids"] = ["problem:historical"]

    result = canonicalize_problem_cases([current, historical], [decision])

    assert len(result) == 1
    assert result[0]["case_id"] == "case:persisted"
    assert result[0]["canonical_problem_id"] == "problem:historical"
    assert result[0]["absorbed_case_ids"] == ["case:current"]
