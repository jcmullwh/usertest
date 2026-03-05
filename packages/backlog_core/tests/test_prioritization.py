"""Tests for backlog_core.prioritization.

These tests cover deterministic, evidence-only pre-scoring used by stage 2.
The contract is intentionally simple:
- Output is deterministic given the same inputs.
- Every input problem record yields one signal object with a bucket candidate and a
  human-readable score breakdown.
"""

from __future__ import annotations

from backlog_core.prioritization import compute_problem_priority_signals


def test_compute_problem_priority_signals_is_deterministic() -> None:
    atoms = [
        {
            "atom_id": "runA:run_failure_event:1",
            "run_id": "runA",
            "agent": "codex",
            "mission_id": "m1",
            "source": "run_failure_event",
        },
        {
            "atom_id": "runB:command_failure:1",
            "run_id": "runB",
            "agent": "claude",
            "mission_id": "m1",
            "source": "command_failure",
        },
        {
            "atom_id": "runA:suggested_change:1",
            "run_id": "runA",
            "agent": "codex",
            "mission_id": "m1",
            "source": "suggested_change",
        },
    ]

    problem_records = [
        {
            "problem_id": "problem:run-failures",
            "title": "Run failures observed",
            "problem": "Runs fail with errors.",
            "user_impact": "Users are blocked from completing runs.",
            "severity": "blocker",
            "confidence": 0.8,
            "evidence_atom_ids": ["runA:run_failure_event:1", "runB:command_failure:1"],
            "evidence_summary": "Two runs show hard failures.",
            "problem_status": "identified",
        },
        {
            "problem_id": "problem:docs-nice-to-have",
            "title": "Docs improvement suggestion",
            "problem": "Docs could be improved.",
            "user_impact": "Minor friction.",
            "severity": "low",
            "confidence": 0.3,
            "evidence_atom_ids": ["runA:suggested_change:1"],
            "evidence_summary": "One suggested-change atom.",
            "problem_status": "identified",
        },
    ]

    first = compute_problem_priority_signals(problem_records, atoms)
    second = compute_problem_priority_signals(problem_records, atoms)
    assert first == second


def test_compute_problem_priority_signals_ranks_blockers_higher() -> None:
    atoms = [
        {
            "atom_id": "runA:run_failure_event:1",
            "run_id": "runA",
            "agent": "codex",
            "mission_id": "m1",
            "source": "run_failure_event",
        },
        {
            "atom_id": "runB:confusion_point:1",
            "run_id": "runB",
            "agent": "claude",
            "mission_id": "m1",
            "source": "confusion_point",
        },
    ]
    problem_records = [
        {
            "problem_id": "problem:blocker",
            "title": "Blocker",
            "problem": "Hard failure.",
            "user_impact": "Users are blocked.",
            "severity": "blocker",
            "confidence": 0.7,
            "evidence_atom_ids": ["runA:run_failure_event:1"],
            "evidence_summary": "Failure event.",
            "problem_status": "identified",
        },
        {
            "problem_id": "problem:minor",
            "title": "Minor",
            "problem": "Confusing text.",
            "user_impact": "Mild confusion.",
            "severity": "low",
            "confidence": 0.7,
            "evidence_atom_ids": ["runB:confusion_point:1"],
            "evidence_summary": "Confusion point.",
            "problem_status": "identified",
        },
    ]

    signals = compute_problem_priority_signals(problem_records, atoms)
    assert len(signals) == 2

    blocker = signals[0]
    minor = signals[1]
    assert blocker["problem_id"] == "problem:blocker"
    assert minor["problem_id"] == "problem:minor"

    assert 0.0 <= float(blocker["pre_score"]) <= 1.0
    assert 0.0 <= float(minor["pre_score"]) <= 1.0
    assert float(blocker["pre_score"]) > float(minor["pre_score"])

    assert blocker["bucket_candidate"] in {"p0", "p1"}
    assert minor["bucket_candidate"] in {"p2", "p3", "watch"}

