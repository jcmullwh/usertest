"""Tests for backlog_core.prioritization.

These tests cover deterministic, evidence-only pre-scoring used by stage 2.
The contract is intentionally simple:
- Output is deterministic given the same inputs.
- Every input problem record yields one signal object with a bucket candidate and a
  human-readable score breakdown.
"""

from __future__ import annotations

from copy import deepcopy

from backlog_core.operational_candidates import build_operational_failure_candidates
from backlog_core.prioritization import compute_problem_priority_signals


def _operational_candidate(*, occurrence_count: int) -> dict[str, object]:
    records: list[dict[str, object]] = []
    atoms: list[dict[str, object]] = []
    for index in range(occurrence_count):
        run_id = f"research/run/{index}"
        case_id = f"case:parent:{index}"
        records.append(
            {
                "run_rel": run_id,
                "status": "error",
                "agent_exit_code": 1,
                "target_ref": {
                    "mission_id": "backlog_repro_research",
                    "report_schema_path": ("configs/report_schemas/troubleshoot_v1.schema.json"),
                },
                "error": {
                    "type": "AgentConfigInvalid",
                    "subtype": "invalid_agent_config",
                    "code": "codex_model_messages_missing",
                    "message": "Different prose must not affect the typed signature.",
                },
                "metrics": {},
                "report_validation_errors": [],
                "terminal_artifact_reads": {},
            }
        )
        atoms.append(
            {
                "atom_id": f"{run_id}:run_failure_event:1",
                "run_id": run_id,
                "run_rel": run_id,
                "origin_run_id": run_id,
                "source": "run_failure_event",
                "text": "Free-form derived text is not ranking evidence.",
                "evidence_class": "observed",
                "evidence_role": "research",
                "origin_stage": "repro_research",
                "parent_case_id": case_id,
                "case_id": case_id,
                "supporting_case_ids": [case_id],
                "disposition": "supports_case",
                "disposition_status": "decided",
                "lineage_authorities": ["runner_evidence_assignment"],
            }
        )
    [candidate] = build_operational_failure_candidates(records, atoms)
    return candidate


def _problem(problem_id: str, evidence_atom_id: str) -> dict[str, object]:
    return {
        "problem_id": problem_id,
        "title": "Execution failure",
        "problem": "An execution path failed.",
        "user_impact": "Users are blocked from completing the run.",
        "severity": "high",
        "confidence": 0.7,
        "evidence_atom_ids": [evidence_atom_id],
        "evidence_summary": "One typed evidence source.",
        "problem_status": "identified",
    }


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


def test_compute_problem_priority_signals_scores_token_monitoring_sources() -> None:
    atoms = [
        {
            "atom_id": "runA:token_monitoring_signal:1",
            "run_id": "runA",
            "agent": "codex",
            "mission_id": "m1",
            "source": "token_monitoring_signal",
        },
        {
            "atom_id": "runB:token_monitoring_error:1",
            "run_id": "runB",
            "agent": "codex",
            "mission_id": "m1",
            "source": "token_monitoring_error",
        },
    ]
    problem_records = [
        {
            "problem_id": "problem:token-monitoring",
            "title": "Token monitoring surfaced waste",
            "problem": "Monitoring identified avoidable context resend.",
            "user_impact": "Runs consume unnecessary tokens.",
            "severity": "high",
            "confidence": 0.8,
            "evidence_atom_ids": [
                "runA:token_monitoring_signal:1",
                "runB:token_monitoring_error:1",
            ],
            "evidence_summary": "Monitoring signal and hook error.",
            "problem_status": "identified",
        }
    ]

    [signal] = compute_problem_priority_signals(problem_records, atoms)
    sources = signal["score_breakdown"]["sources"]
    assert sources["source_counts"] == {
        "token_monitoring_error": 1,
        "token_monitoring_signal": 1,
    }
    assert sources["source_strength_score"] == 0.775


def test_verified_multi_occurrence_operational_candidate_outranks_transient_failure() -> None:
    candidate = _operational_candidate(occurrence_count=3)
    transient = {
        "atom_id": "run:transient:command_failure:1",
        "run_id": "run:transient",
        "agent": "codex",
        "mission_id": "ordinary_task",
        "source": "command_failure",
    }
    candidate_id = str(candidate["atom_id"])
    problems = [
        _problem("problem:recurring-operational", candidate_id),
        _problem("problem:single-transient", str(transient["atom_id"])),
    ]
    signals = compute_problem_priority_signals(problems, [candidate, transient])

    recurring, one_off = signals
    assert signals == compute_problem_priority_signals(problems, [candidate, transient])
    assert recurring["pre_score"] > one_off["pre_score"]
    assert recurring["score_breakdown"]["operational_candidates"] == {
        "verified_receipts": 1,
        "unverified_receipts": 0,
        "verified_occurrences": 3,
        "distinct_source_runs": 3,
    }
    assert recurring["score_breakdown"]["breadth"]["distinct_runs"] == 3
    assert recurring["score_breakdown"]["recurrence"]["effective_observations"] == 3
    assert recurring["score_breakdown"]["sources"]["source_strength_score"] == 1.0


def test_tampered_operational_receipt_adds_no_occurrence_or_source_run_strength() -> None:
    candidate = _operational_candidate(occurrence_count=3)
    tampered = deepcopy(candidate)
    tampered["operational_candidate_receipt"]["occurrence_count"] = 300
    transient = {
        "atom_id": "run:transient:command_failure:1",
        "run_id": "run:transient",
        "agent": "codex",
        "mission_id": "ordinary_task",
        "source": "command_failure",
    }
    candidate_id = str(tampered["atom_id"])
    signals = compute_problem_priority_signals(
        [
            _problem("problem:tampered", candidate_id),
            _problem("problem:single-transient", str(transient["atom_id"])),
        ],
        [tampered, transient],
    )

    tampered_signal, one_off = signals
    assert tampered_signal["pre_score"] < one_off["pre_score"]
    assert tampered_signal["score_breakdown"]["operational_candidates"] == {
        "verified_receipts": 0,
        "unverified_receipts": 1,
        "verified_occurrences": 0,
        "distinct_source_runs": 0,
    }
    assert tampered_signal["score_breakdown"]["breadth"]["distinct_runs"] == 0
    assert tampered_signal["score_breakdown"]["recurrence"]["effective_observations"] == 0
    # The source label by itself is not enough to claim typed-source strength.
    assert tampered_signal["score_breakdown"]["sources"]["source_strength_score"] == 0.5
