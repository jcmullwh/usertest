from __future__ import annotations

import pytest

import backlog_miner.prompt_correction as prompt_correction
from backlog_miner.prompt_correction import (
    CorrectionFrontier,
    CorrectionObservation,
    CorrectionRunResult,
    acquire_author_session,
    correction_metrics_with_session_acquisition,
    correction_run_metrics,
    correction_state_sha256,
    run_progressive_correction,
)


def _observation(
    payload: str,
    errors: list[str],
    *,
    keys: list[str] | None = None,
    cost: float = 0.0,
    session_id: str | None = "019f2cca-9011-7e32-88ae-6c25af578b49",
) -> CorrectionObservation[str]:
    valid_keys = tuple(keys or [])
    return CorrectionObservation(
        payload=payload,
        validation_errors=tuple(errors),
        state_sha256=correction_state_sha256(
            candidate=payload,
            validation_errors=errors,
            valid_item_keys=list(valid_keys),
        ),
        valid_item_keys=valid_keys,
        agent_session_id=session_id,
        continuity_key="workspace-and-input",
        cost_seconds=cost,
    )


def test_two_old_errors_to_one_new_error_is_forward_progress() -> None:
    tracker = CorrectionFrontier(_observation("initial", ["old:a", "old:b"]))

    assessment = tracker.assess(_observation("revised", ["new:c"]))

    assert assessment.decision == "continue"
    assert assessment.reason == "error_count_decreased"
    assert assessment.global_best_updated is True
    assert assessment.reset_progress_clock is True
    assert assessment.introduced_error_identities == ("new:c",)
    assert tracker.current.payload == "revised"
    assert tracker.best.payload == "revised"


def test_duplicate_validator_diagnostics_do_not_change_state_or_progress_counts() -> None:
    assert correction_state_sha256(
        candidate="same",
        validation_errors=["shape:error", " shape:error  "],
    ) == correction_state_sha256(
        candidate="same",
        validation_errors=["shape:error"],
    )
    tracker = CorrectionFrontier(
        _observation("initial", ["old:a", " old:a  "])
    )

    assessment = tracker.assess(_observation("revised", ["new:b", "new:b"]))

    assert assessment.before_error_count == 1
    assert assessment.after_error_count == 1
    assert assessment.reason == "prior_error_resolved"
    assert assessment.global_best_updated is False


def test_one_old_error_to_one_new_error_advances_current_not_objective_best() -> None:
    tracker = CorrectionFrontier(_observation("initial", ["old:a"]))

    assessment = tracker.assess(_observation("latest", ["new:b"]))

    assert assessment.reason == "prior_error_resolved"
    assert assessment.global_best_updated is False
    assert assessment.reset_progress_clock is True
    assert tracker.current.payload == "latest"
    assert tracker.best.payload == "initial"


def test_one_old_error_to_many_new_errors_does_not_overwrite_objective_best() -> None:
    tracker = CorrectionFrontier(_observation("initial", ["old:a"]))
    many_errors = [f"new:{index}" for index in range(100)]

    assessment = tracker.assess(_observation("latest", many_errors))

    assert assessment.reason == "prior_error_resolved"
    assert assessment.global_best_updated is False
    assert tracker.current.payload == "latest"
    assert tracker.best.payload == "initial"


def test_progress_resets_global_cost_clock_before_caller_pause() -> None:
    initial = _observation("initial", ["old:a", "old:b"], cost=100.0)
    candidates = iter(
        [
            _observation("frontier", ["new:c"], cost=60.0),
            _observation("new-shape-same-error", ["new:c"], cost=100.0),
        ]
    )

    result = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda _current, _number, _feedback: next(candidates),
        pause_policy=lambda _current, _assessment, since_progress, _total: (
            "correction_cost_reached_original" if since_progress >= initial.cost_seconds else None
        ),
    )

    assert result.status == "repairable_paused:correction_cost_reached_original"
    assert result.best.payload == "frontier"
    assert result.assessments[0].reset_progress_clock is True
    assert result.correction_cost_since_progress == 100.0
    assert result.total_correction_cost == 160.0


def test_changed_state_with_same_error_becomes_next_feedback_frontier() -> None:
    initial = _observation("initial", ["shape:error"])
    changed = _observation("changed but still invalid", ["shape:error"])
    observed_currents: list[str] = []
    candidates = iter([changed, _observation("valid", [])])

    result = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda current, _number, _feedback: (
            observed_currents.append(current.payload) or next(candidates)
        ),
    )

    assert result.status == "corrected"
    assert observed_currents == ["initial", "changed but still invalid"]
    assert result.assessments[0].reason == "new_state_remains_repairable"
    assert result.assessments[0].safe_frontier_updated is True


def test_explicit_resume_continues_retained_frontier_cost_and_attempt_number() -> None:
    initial = _observation("initial", ["old:a", "old:b"], cost=5.0)
    paused = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda _current, _number, _feedback: _observation(
            "fewer-errors",
            ["new:c"],
            cost=5.0,
        ),
        pause_policy=lambda _current, _assessment, since_progress, _total: (
            "explicit_score_boundary" if since_progress >= 0.0 else None
        ),
    )
    calls: list[tuple[str, int, str | None]] = []

    resumed = run_progressive_correction(
        initial=paused.current,
        resume_from=paused,
        invoke_correction=lambda current, number, assessment: (
            calls.append(
                (
                    current.payload,
                    number,
                    assessment.reason if assessment is not None else None,
                )
            )
            or _observation("valid", [], cost=2.0)
        ),
    )

    assert paused.status == "repairable_paused:explicit_score_boundary"
    assert resumed.status == "corrected"
    assert calls == [("fewer-errors", 3, "error_count_decreased")]
    assert [attempt.payload for attempt in resumed.attempts] == [
        "initial",
        "fewer-errors",
        "valid",
    ]
    assert resumed.total_correction_cost == 7.0


def test_explicit_resume_preserves_cross_process_recurrence_history() -> None:
    initial = _observation("A", ["error:a"], cost=1.0)
    paused = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda _current, _number, _feedback: _observation(
            "B", ["error:b"], cost=1.0
        ),
        pause_policy=lambda _current, _assessment, _since, _total: "score_boundary",
    )

    resumed = run_progressive_correction(
        initial=paused.current,
        resume_from=paused,
        invoke_correction=lambda _current, _number, _feedback: initial,
    )

    assert resumed.status == "stalled:previous_state_recurred"
    assert resumed.assessments[-1].repeated_state is True


def test_explicit_resume_preserves_consecutive_noop_history() -> None:
    initial = _observation("A", ["error:a"], cost=1.0)
    paused = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda _current, _number, _feedback: initial,
        pause_policy=lambda _current, _assessment, _since, _total: "score_boundary",
    )

    resumed = run_progressive_correction(
        initial=paused.current,
        resume_from=paused,
        invoke_correction=lambda _current, _number, _feedback: initial,
    )

    assert resumed.status == "stalled:same_state_repeated_after_feedback"


def test_global_best_never_regresses_after_local_error_reduction() -> None:
    tracker = CorrectionFrontier(_observation("three", ["e:1", "e:2", "e:3"]))
    tracker.assess(_observation("two", ["e:4", "e:5"]))
    tracker.assess(_observation("hundred", [f"wide:{index}" for index in range(100)]))

    assessment = tracker.assess(
        _observation("ninety-nine", [f"wide:{index}" for index in range(99)])
    )

    assert assessment.reason == "error_count_decreased"
    assert assessment.safe_frontier_updated is True
    assert assessment.global_best_updated is False
    assert tracker.current.payload == "ninety-nine"
    assert tracker.best.payload == "two"


def test_exact_a_b_a_recurrence_stalls_before_identity_progress_can_mask_cycle() -> None:
    tracker = CorrectionFrontier(_observation("A", ["error:a"]))
    first = tracker.assess(_observation("B", ["error:b"]))

    second = tracker.assess(_observation("A", ["error:a"]))

    assert first.decision == "continue"
    assert first.reason == "prior_error_resolved"
    assert second.decision == "stalled"
    assert second.reason == "previous_state_recurred"


def test_first_exact_noop_gets_feedback_before_second_noop_stalls() -> None:
    initial = _observation("A", ["error:a"])
    tracker = CorrectionFrontier(initial)

    first = tracker.assess(initial)
    second = tracker.assess(initial)

    assert first.decision == "continue"
    assert first.reason == "first_noop_receives_feedback"
    assert second.decision == "stalled"
    assert second.reason == "same_state_repeated_after_feedback"


def test_unknown_validator_identity_is_repairable_without_allowlist() -> None:
    tracker = CorrectionFrontier(
        _observation("A", ["future_validator_alpha: unforeseen shape"])
    )

    assessment = tracker.assess(
        _observation("B", ["future_validator_beta: newly exposed child"])
    )

    assert assessment.decision == "continue"
    assert assessment.reason == "prior_error_resolved"
    assert assessment.safe_frontier_updated is True
    assert assessment.global_best_updated is False
    assert assessment.reset_progress_clock is True
    assert assessment.resolved_error_identities == (
        "future_validator_alpha: unforeseen shape",
    )


def test_replaced_error_resets_cost_clock_until_identity_progress_stops() -> None:
    initial = _observation("initial", ["old:error"], cost=50.0)
    candidates = iter(
        [
            _observation("replacement", ["new:error"], cost=40.0),
            _observation("same-frontier-new-shape", ["new:error"], cost=50.0),
        ]
    )

    result = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda _current, _number, _feedback: next(candidates),
        pause_policy=lambda _current, _assessment, since_progress, _total: (
            "correction_cost_reached_original" if since_progress >= initial.cost_seconds else None
        ),
    )

    assert result.status == "repairable_paused:correction_cost_reached_original"
    assert result.assessments[0].reason == "prior_error_resolved"
    assert result.assessments[0].reset_progress_clock is True
    assert result.assessments[1].reason == "new_state_remains_repairable"
    assert result.assessments[1].reset_progress_clock is False
    assert result.correction_cost_since_progress == 50.0
    assert result.total_correction_cost == 90.0


def test_distinct_contract_churn_pauses_and_retains_the_complete_frontier() -> None:
    initial = _observation("initial", ["contract:original"], cost=5.0)
    candidates = iter(
        [
            _observation("revision-one", ["contract:replacement-one"], cost=1.0),
            _observation("revision-two", ["contract:replacement-two"], cost=1.0),
            _observation("revision-three", ["contract:replacement-three"], cost=1.0),
        ]
    )

    result = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda _current, _number, _feedback: next(candidates),
    )

    assert result.status == (
        "repairable_paused:"
        "consecutive_nonadvancing_corrections_require_adjudication"
    )
    assert [attempt.payload for attempt in result.attempts] == [
        "initial",
        "revision-one",
        "revision-two",
        "revision-three",
    ]
    assert result.current.payload == "revision-three"
    assert result.best.payload == "initial"
    assert len(result.assessments) == 3
    assert result.total_correction_cost == 3.0

    resumed = run_progressive_correction(
        initial=result.current,
        resume_from=result,
        invoke_correction=lambda _current, _number, _feedback: _observation(
            "schema-corrected",
            [],
            cost=1.0,
        ),
    )

    assert resumed.status == "corrected"
    assert [attempt.payload for attempt in resumed.attempts][-2:] == [
        "revision-three",
        "schema-corrected",
    ]


def test_stage5_shape_accepts_valid_fourth_output_after_two_replacement_errors() -> None:
    candidates = iter(
        [
            _observation("stage5-revision-one", ["selection:replacement-one"]),
            _observation("stage5-revision-two", ["selection:replacement-two"]),
            _observation("stage5-valid", []),
        ]
    )

    result = run_progressive_correction(
        initial=_observation("stage5-initial", ["selection:initial"]),
        invoke_correction=lambda _current, _number, _feedback: next(candidates),
    )

    assert result.status == "corrected"
    assert [attempt.payload for attempt in result.attempts] == [
        "stage5-initial",
        "stage5-revision-one",
        "stage5-revision-two",
        "stage5-valid",
    ]
    assert [assessment.reason for assessment in result.assessments] == [
        "prior_error_resolved",
        "prior_error_resolved",
        "output_contract_satisfied",
    ]


def test_material_current_progress_has_no_low_correction_turn_cap() -> None:
    initial_errors = [f"contract:{index}" for index in range(6)]
    candidates = iter(
        [
            _observation(
                f"revision-{remaining}",
                [f"replacement:{index}" for index in range(remaining)],
                cost=1.0,
            )
            for remaining in range(5, -1, -1)
        ]
    )

    result = run_progressive_correction(
        initial=_observation("initial", initial_errors, cost=5.0),
        invoke_correction=lambda _current, _number, _feedback: next(candidates),
    )

    assert result.status == "corrected"
    assert len(result.attempts) == 7
    assert len(result.assessments) == 6
    assert all(
        assessment.after_error_count < assessment.before_error_count
        for assessment in result.assessments[:-1]
    )


def test_metrics_expose_useful_acceptance_and_false_rejection_denominators() -> None:
    initial = _observation("initial", ["shape:error"])
    accepted = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda _current, _number, _feedback: _observation("valid", []),
    )
    stalled = run_progressive_correction(
        initial=initial,
        invoke_correction=lambda current, _number, _feedback: current,
    )

    accepted_metrics = correction_run_metrics(accepted, expected_quality="good")
    stalled_metrics = correction_run_metrics(stalled, expected_quality="good")
    bad_metrics = correction_run_metrics(accepted, expected_quality="bad")

    assert accepted_metrics["accepted_good"] is True
    assert accepted_metrics["repaired"] is True
    assert stalled_metrics["accepted_good"] is False
    assert stalled_metrics["false_rejected"] is True
    assert bad_metrics["accepted_bad"] is True


def test_pre_author_transient_failure_can_acquire_fresh_session() -> None:
    initial = _observation("transport failed", ["transport:x"], cost=1.0, session_id=None)
    calls: list[int] = []

    acquisition = acquire_author_session(
        initial=initial,
        invoke_fresh=lambda number: (
            calls.append(number)
            or _observation("valid", [], cost=0.5)
        ),
    )

    assert acquisition.status == "acquired"
    assert calls == [2]
    assert len(acquisition.attempts) == 2
    assert acquisition.current.agent_session_id is not None


def test_pre_author_equivalent_failure_pauses_with_all_receipts() -> None:
    initial = _observation("same", ["transport:x"], cost=1.0, session_id=None)

    acquisition = acquire_author_session(
        initial=initial,
        invoke_fresh=lambda _number: _observation(
            "same",
            ["transport:x"],
            cost=0.5,
            session_id=None,
        ),
    )

    assert acquisition.status == (
        "repairable_paused:author_session_acquisition_repeated"
    )
    assert len(acquisition.attempts) == 2


def test_pre_author_duration_never_becomes_a_fresh_retry_budget() -> None:
    initial = _observation(
        "first-failure",
        ["transport:first"],
        cost=0.1,
        session_id=None,
    )
    candidates = iter(
        [
            _observation(
                "different-failure",
                ["transport:second"],
                cost=20.0,
                session_id=None,
            ),
            _observation("valid", [], cost=0.5),
        ]
    )

    acquisition = acquire_author_session(
        initial=initial,
        invoke_fresh=lambda _number: next(candidates),
    )

    assert acquisition.status == "acquired"
    assert len(acquisition.attempts) == 3


def test_acquisition_metrics_do_not_call_fresh_retries_correction_turns() -> None:
    initial = _observation("same", ["transport:x"], cost=10.0, session_id=None)
    acquisition = acquire_author_session(
        initial=initial,
        invoke_fresh=lambda _number: _observation(
            "same",
            ["transport:x"],
            cost=5.0,
            session_id=None,
        ),
    )
    paused = CorrectionRunResult(
        status=acquisition.status,
        current=acquisition.current,
        best=acquisition.best,
        attempts=acquisition.attempts,
        assessments=(),
        total_correction_cost=5.0,
    )

    metrics = correction_metrics_with_session_acquisition(
        correction_run_metrics(paused, expected_quality=None),
        acquisition,
    )

    assert metrics["attempt_count"] == 2
    assert metrics["correction_turn_count"] == 0
    assert metrics["session_acquisition_retry_count"] == 1
    assert metrics["session_acquisition_cost_seconds"] == 15.0
    assert metrics["total_correction_cost_seconds"] == 0.0
    assert metrics["total_elapsed_seconds"] == 15.0


def test_pre_author_invocation_exception_is_retained_then_retried_fresh() -> None:
    initial = _observation("initial failure", ["transport:x"], session_id=None)
    calls: list[int] = []

    def invoke(number: int) -> CorrectionObservation[str]:
        calls.append(number)
        if number == 2:
            raise RuntimeError("transient transport loss")
        return _observation("valid", [])

    acquisition = acquire_author_session(initial=initial, invoke_fresh=invoke)

    assert acquisition.status == "acquired"
    assert calls == [2, 3]
    assert len(acquisition.invocation_failures) == 1
    failure = acquisition.invocation_failures[0]
    assert failure.attempt_number == 2
    assert failure.error_type == "RuntimeError"
    assert failure.error_message == "transient transport loss"
    accepted = run_progressive_correction(
        initial=acquisition.current,
        invoke_correction=lambda *_args: acquisition.current,
    )
    metrics = correction_metrics_with_session_acquisition(
        correction_run_metrics(accepted, expected_quality=None),
        acquisition,
    )
    assert metrics["attempt_count"] == 3
    assert metrics["session_acquisition_retry_count"] == 2
    assert metrics["session_acquisition_failure_count"] == 1


def test_pre_author_equivalent_invocation_exception_pauses_on_recurrence() -> None:
    initial = _observation("initial failure", ["transport:x"], session_id=None)
    calls: list[int] = []

    def invoke(number: int) -> CorrectionObservation[str]:
        calls.append(number)
        raise RuntimeError("persistent transport loss")

    acquisition = acquire_author_session(initial=initial, invoke_fresh=invoke)

    assert acquisition.status == (
        "repairable_paused:author_session_acquisition_invocation_repeated"
    )
    assert calls == [2, 3]
    assert len(acquisition.invocation_failures) == 2
    assert (
        acquisition.invocation_failures[0].failure_identity
        == acquisition.invocation_failures[1].failure_identity
    )


def test_transient_exact_session_invocation_exception_retries_same_author() -> None:
    initial = _observation("initial", ["shape:error"], cost=1.0)
    calls: list[tuple[int, str | None]] = []

    def invoke(
        current: CorrectionObservation[str],
        number: int,
        _assessment: object,
    ) -> CorrectionObservation[str]:
        calls.append((number, current.agent_session_id))
        if number == 2:
            raise RuntimeError("transient continuation loss")
        return _observation("valid", [], cost=0.5)

    result = run_progressive_correction(
        initial=initial,
        invoke_correction=invoke,
    )

    assert result.status == "corrected"
    assert calls == [(2, initial.agent_session_id), (3, initial.agent_session_id)]
    assert len(result.invocation_failures) == 1
    assert result.invocation_failures[0].attempt_number == 2
    metrics = correction_run_metrics(result, expected_quality=None)
    assert metrics["attempt_count"] == 3
    assert metrics["correction_turn_count"] == 1
    assert metrics["correction_invocation_failure_count"] == 1


def test_repeated_exact_session_invocation_exception_pauses_on_recurrence() -> None:
    initial = _observation("initial", ["shape:error"], cost=1.0)
    calls: list[tuple[int, str | None]] = []

    def invoke(
        current: CorrectionObservation[str],
        number: int,
        _assessment: object,
    ) -> CorrectionObservation[str]:
        calls.append((number, current.agent_session_id))
        raise RuntimeError("persistent continuation loss")

    result = run_progressive_correction(
        initial=initial,
        invoke_correction=invoke,
    )

    assert result.status == "repairable_paused:correction_invocation_repeated"
    assert calls == [(2, initial.agent_session_id), (3, initial.agent_session_id)]
    assert result.current is initial
    assert result.best is initial
    assert len(result.invocation_failures) == 2
    assert (
        result.invocation_failures[0].failure_identity
        == result.invocation_failures[1].failure_identity
    )


def test_changing_transport_failures_consume_economic_pause_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _observation("initial", ["shape:error"], cost=10.0)
    monotonic_values = iter([0.0, 6.0, 6.0, 12.0])
    monkeypatch.setattr(
        prompt_correction.time,
        "monotonic",
        lambda: next(monotonic_values),
    )
    calls: list[int] = []

    def invoke(
        _current: CorrectionObservation[str],
        number: int,
        _assessment: object,
    ) -> CorrectionObservation[str]:
        calls.append(number)
        raise RuntimeError(f"transport failure {number}")

    result = run_progressive_correction(
        initial=initial,
        invoke_correction=invoke,
        pause_policy=lambda _current, _assessment, since_progress, _total: (
            "correction_cost_reached_original"
            if since_progress >= initial.cost_seconds
            else None
        ),
    )

    assert result.status == "repairable_paused:correction_cost_reached_original"
    assert calls == [2, 3]
    assert len(result.invocation_failures) == 2
    assert (
        result.invocation_failures[0].failure_identity
        != result.invocation_failures[1].failure_identity
    )
    assert result.current is initial
    assert result.best is initial
    assert result.correction_cost_since_progress == 12.0
    assert result.total_correction_cost == 12.0
    metrics = correction_run_metrics(result, expected_quality=None)
    assert metrics["correction_invocation_failure_cost_seconds"] == 12.0
    assert metrics["total_correction_cost_seconds"] == 12.0
    assert metrics["total_elapsed_seconds"] == 22.0
