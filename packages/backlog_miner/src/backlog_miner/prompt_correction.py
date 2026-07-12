"""Stage-neutral progress policy for same-author model-output correction.

The policy deliberately knows nothing about research dossiers, problem records, or plans.
Callers provide deterministic validation errors, stable valid-item keys, and a hash of the
complete candidate state.  That keeps transport/session continuity and progress semantics
consistent across backlog stages without turning one benchmark shape into a special case.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Generic, TypeVar

T = TypeVar("T")


def normalize_validation_error(error: str) -> str:
    """Normalize presentation whitespace while preserving an unknown error's identity."""

    return " ".join(str(error).split())


def normalized_validation_error_identities(
    errors: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Return stable unique validator identities for all progress arithmetic."""

    return tuple(sorted({normalize_validation_error(error) for error in errors}))


def correction_state_sha256(
    *,
    candidate: object,
    validation_errors: tuple[str, ...] | list[str],
    valid_item_keys: tuple[str, ...] | list[str] = (),
) -> str:
    """Content-address one candidate, its exact validator frontier, and valid keyed items."""

    payload = {
        "candidate": candidate,
        "validation_errors": list(
            normalized_validation_error_identities(validation_errors)
        ),
        "valid_item_keys": sorted(set(str(key) for key in valid_item_keys)),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CorrectionObservation(Generic[T]):
    """One retained model output and its deterministic validation projection."""

    payload: T
    validation_errors: tuple[str, ...]
    state_sha256: str
    valid_item_keys: tuple[str, ...] = ()
    agent_session_id: str | None = None
    continuity_key: str | None = None
    cost_seconds: float = 0.0


@dataclass(frozen=True)
class CorrectionAssessment:
    """Decision made after comparing a correction with the active safe frontier."""

    decision: str
    reason: str
    resolved_error_identities: tuple[str, ...]
    introduced_error_identities: tuple[str, ...]
    before_error_count: int
    after_error_count: int
    repeated_state: bool
    safe_frontier_updated: bool
    global_best_updated: bool
    reset_progress_clock: bool


@dataclass(frozen=True)
class CorrectionRunResult(Generic[T]):
    """Terminal result of an adaptive correction conversation."""

    status: str
    current: CorrectionObservation[T]
    best: CorrectionObservation[T]
    attempts: tuple[CorrectionObservation[T], ...]
    assessments: tuple[CorrectionAssessment, ...]
    invocation_failures: tuple[CorrectionInvocationFailure, ...] = ()
    operational_error: str | None = None
    correction_cost_since_progress: float = 0.0
    total_correction_cost: float = 0.0


@dataclass(frozen=True)
class AuthorSessionAcquisitionResult(Generic[T]):
    """Fresh pre-author attempts made before any resumable session exists."""

    status: str
    current: CorrectionObservation[T]
    best: CorrectionObservation[T]
    attempts: tuple[CorrectionObservation[T], ...]
    invocation_failures: tuple[AuthorSessionAcquisitionFailure, ...] = ()
    cost_since_progress: float = 0.0
    total_cost: float = 0.0


@dataclass(frozen=True)
class AuthorSessionAcquisitionFailure:
    """One fresh invocation that failed before it could return an observation."""

    attempt_number: int
    error_type: str
    error_message: str
    failure_identity: str
    cost_seconds: float


@dataclass(frozen=True)
class CorrectionInvocationFailure:
    """One failed exact-session continuation invocation."""

    attempt_number: int
    agent_session_id: str
    error_type: str
    error_message: str
    failure_identity: str
    cost_seconds: float


class CorrectionFrontier(Generic[T]):
    """Track one exact-session correction chain without an arbitrary turn cap."""

    def __init__(self, initial: CorrectionObservation[T]) -> None:
        self.current = initial
        self.best = initial
        self._seen_states = {initial.state_sha256}
        self._same_state_noop_count = 0

    @classmethod
    def resume(
        cls,
        *,
        current: CorrectionObservation[T],
        best: CorrectionObservation[T],
        attempts: tuple[CorrectionObservation[T], ...],
        assessments: tuple[CorrectionAssessment, ...],
    ) -> CorrectionFrontier[T]:
        """Restore the complete safe frontier for an explicit later continuation.

        A paused correction is not a new correction run. Restoring all observed states is
        what makes A -> B -> pause -> A remain a confirmed recurrence instead of appearing
        to be novel progress after process restart. The consecutive no-op counter is likewise
        reconstructed from the retained assessment tail.
        """

        if not attempts:
            raise ValueError("correction_resume_attempts_missing")
        attempt_states = {attempt.state_sha256 for attempt in attempts}
        if current.state_sha256 not in attempt_states:
            raise ValueError("correction_resume_current_not_in_attempts")
        if best.state_sha256 not in attempt_states:
            raise ValueError("correction_resume_best_not_in_attempts")
        frontier = cls(attempts[0])
        frontier.current = current
        frontier.best = best
        frontier._seen_states = attempt_states
        frontier._same_state_noop_count = 0
        for assessment in reversed(assessments):
            if (
                assessment.repeated_state
                and assessment.reason == "first_noop_receives_feedback"
            ):
                frontier._same_state_noop_count += 1
                continue
            break
        return frontier

    @staticmethod
    def _error_ids(errors: tuple[str, ...]) -> set[str]:
        return set(normalized_validation_error_identities(errors))

    def assess(self, candidate: CorrectionObservation[T]) -> CorrectionAssessment:
        """Assess one candidate, updating the safe frontier only after continuity checks."""

        before = self.current
        before_ids = self._error_ids(before.validation_errors)
        after_ids = self._error_ids(candidate.validation_errors)
        resolved = tuple(sorted(before_ids - after_ids))
        introduced = tuple(sorted(after_ids - before_ids))

        def result(
            decision: str,
            reason: str,
            *,
            repeated_state: bool = False,
            frontier_updated: bool = False,
            best_updated: bool = False,
        ) -> CorrectionAssessment:
            return CorrectionAssessment(
                decision=decision,
                reason=reason,
                resolved_error_identities=resolved,
                introduced_error_identities=introduced,
                before_error_count=len(before_ids),
                after_error_count=len(after_ids),
                repeated_state=repeated_state,
                safe_frontier_updated=frontier_updated,
                global_best_updated=best_updated,
                reset_progress_clock=best_updated,
            )

        if (
            before.agent_session_id is not None
            and candidate.agent_session_id != before.agent_session_id
        ):
            return result("continuity_failed", "agent_session_changed")
        if before.continuity_key is not None and candidate.continuity_key != before.continuity_key:
            return result("continuity_failed", "workspace_or_input_continuity_changed")

        if not candidate.validation_errors:
            self.current = candidate
            self.best = candidate
            self._seen_states.add(candidate.state_sha256)
            self._same_state_noop_count = 0
            return result(
                "accepted",
                "output_contract_satisfied",
                frontier_updated=True,
                best_updated=True,
            )

        if candidate.state_sha256 == before.state_sha256:
            # One no-op receives exact feedback. A second consecutive no-op is clear nonprogress.
            self._same_state_noop_count += 1
            if self._same_state_noop_count >= 2:
                return result(
                    "stalled",
                    "same_state_repeated_after_feedback",
                    repeated_state=True,
                )
            self.current = candidate
            return result("continue", "first_noop_receives_feedback", repeated_state=True)

        self._same_state_noop_count = 0
        if candidate.state_sha256 in self._seen_states:
            # Check recurrence before error-identity progress so A -> B -> A cannot loop forever.
            return result("stalled", "previous_state_recurred", repeated_state=True)

        before_keys = set(before.valid_item_keys)
        after_keys = set(candidate.valid_item_keys)
        best_ids = self._error_ids(self.best.validation_errors)
        valid_items_increased = len(after_keys) > len(before_keys) or after_keys > before_keys
        net_improvement = bool(
            len(after_ids) < len(before_ids)
            or valid_items_increased
        )
        best_keys = set(self.best.valid_item_keys)
        objective_best_improved = bool(
            len(after_ids) < len(best_ids)
            or (
                len(after_ids) == len(best_ids)
                and after_keys > best_keys
            )
        )
        frontier_advanced = bool(net_improvement or resolved)
        self._seen_states.add(candidate.state_sha256)
        self.current = candidate
        if frontier_advanced:
            # A resolved parent error may expose multiple child errors. Keep that output as
            # the latest feedback frontier, but retain the objective best unless errors
            # actually decrease or independently valid keyed work increases.
            if objective_best_improved:
                self.best = candidate
            reason = (
                "error_count_decreased"
                if len(after_ids) < len(before_ids)
                else "prior_error_resolved"
                if resolved
                else "valid_keyed_items_increased"
            )
            return result(
                "continue",
                reason,
                frontier_updated=True,
                best_updated=objective_best_improved,
            )

        # A new output state with the same validator frontier is not proof of incapacity.
        # Model corrections are nondeterministic and may need several structurally distinct
        # turns before a validator identity changes. Exact recurrence is handled above; an
        # external operational budget may pause while preserving this frontier.
        return result(
            "continue",
            "new_state_remains_repairable",
            frontier_updated=True,
            best_updated=objective_best_improved,
        )


def acquire_author_session(
    *,
    initial: CorrectionObservation[T],
    invoke_fresh: Callable[[int], CorrectionObservation[T]],
) -> AuthorSessionAcquisitionResult[T]:
    """Acquire an author session without imposing an arbitrary retry count.

    A transport attempt made before a session UUID exists contains no author work, so a
    fresh invocation is safe.  Once an invocation returns a UUID, callers must use only
    exact-session continuation. Equivalent failure recurrence pauses the frontier while
    preserving every receipt. Direct invocation exceptions are content-addressed and retried
    until the same failure identity recurs. A failed pre-session call is not completed author
    work, so its duration is telemetry rather than a rework budget.
    """

    if initial.agent_session_id is not None:
        return AuthorSessionAcquisitionResult(
            status="acquired",
            current=initial,
            best=initial,
            attempts=(initial,),
            total_cost=max(0.0, float(initial.cost_seconds)),
        )
    attempts = [initial]
    invocation_failures: list[AuthorSessionAcquisitionFailure] = []
    best = initial
    seen_states = {initial.state_sha256}
    seen_failure_identities: set[str] = set()
    total_cost = max(0.0, float(initial.cost_seconds))
    cost_since_progress = 0.0
    attempt_number = 2
    while True:
        invocation_started = time.monotonic()
        try:
            candidate = invoke_fresh(attempt_number)
        except Exception as exc:  # noqa: BLE001 - retain and compare pre-author failures
            failure_cost = max(0.0, time.monotonic() - invocation_started)
            total_cost += failure_cost
            cost_since_progress += failure_cost
            error_type = type(exc).__name__
            error_message = normalize_validation_error(str(exc))
            failure_identity = correction_state_sha256(
                candidate={
                    "phase": "author_session_acquisition",
                    "error_type": error_type,
                    "error_message": error_message,
                },
                validation_errors=[f"{error_type}: {error_message}"],
            )
            invocation_failures.append(
                AuthorSessionAcquisitionFailure(
                    attempt_number=attempt_number,
                    error_type=error_type,
                    error_message=error_message,
                    failure_identity=failure_identity,
                    cost_seconds=failure_cost,
                )
            )
            if failure_identity in seen_failure_identities:
                return AuthorSessionAcquisitionResult(
                    status=(
                        "repairable_paused:"
                        "author_session_acquisition_invocation_repeated"
                    ),
                    current=best,
                    best=best,
                    attempts=tuple(attempts),
                    invocation_failures=tuple(invocation_failures),
                    cost_since_progress=cost_since_progress,
                    total_cost=total_cost,
                )
            seen_failure_identities.add(failure_identity)
            attempt_number += 1
            continue
        attempts.append(candidate)
        candidate_cost = max(0.0, float(candidate.cost_seconds))
        total_cost += candidate_cost
        if candidate.agent_session_id is not None:
            return AuthorSessionAcquisitionResult(
                status="acquired",
                current=candidate,
                best=candidate,
                attempts=tuple(attempts),
                invocation_failures=tuple(invocation_failures),
                cost_since_progress=0.0,
                total_cost=total_cost,
            )
        if candidate.state_sha256 in seen_states:
            return AuthorSessionAcquisitionResult(
                status="repairable_paused:author_session_acquisition_repeated",
                current=candidate,
                best=candidate,
                attempts=tuple(attempts),
                invocation_failures=tuple(invocation_failures),
                cost_since_progress=cost_since_progress,
                total_cost=total_cost,
            )
        seen_states.add(candidate.state_sha256)
        valid_items_increased = len(candidate.valid_item_keys) > len(best.valid_item_keys)
        error_count_decreased = len(
            normalized_validation_error_identities(candidate.validation_errors)
        ) < len(normalized_validation_error_identities(best.validation_errors))
        if valid_items_increased or error_count_decreased:
            best = candidate
            cost_since_progress = 0.0
        else:
            best = candidate
            cost_since_progress += candidate_cost
        attempt_number += 1


def correction_run_metrics(
    result: CorrectionRunResult[object],
    *,
    expected_quality: str | None = None,
) -> dict[str, object]:
    """Project one run into honest throughput/safety benchmark counters.

    Runtime calls normally leave ``expected_quality`` unset because ground truth is not known.
    Labeled benchmarks pass ``"good"`` or ``"bad"`` so useful acceptance, unsafe acceptance,
    and false rejection have explicit denominators instead of being inferred from green tests.
    """

    if expected_quality not in {None, "good", "bad"}:
        raise ValueError("expected_quality must be None, 'good', or 'bad'")
    accepted = result.status in {"accepted", "corrected"}
    invocation_failure_cost = sum(
        max(0.0, float(failure.cost_seconds))
        for failure in result.invocation_failures
    )
    return {
        "status": result.status,
        "attempt_count": len(result.attempts) + len(result.invocation_failures),
        "correction_turn_count": max(0, len(result.attempts) - 1),
        "correction_invocation_failure_count": len(result.invocation_failures),
        "correction_invocation_failure_cost_seconds": invocation_failure_cost,
        "accepted": accepted,
        "accepted_good": (accepted if expected_quality == "good" else None),
        "accepted_bad": (accepted if expected_quality == "bad" else None),
        "false_rejected": ((not accepted) if expected_quality == "good" else None),
        "repaired": result.status == "corrected",
        "stalled": result.status.startswith("stalled:"),
        "repairable_paused": result.status.startswith("repairable_paused:"),
        "initial_cost_seconds": max(0.0, float(result.attempts[0].cost_seconds)),
        "total_correction_cost_seconds": result.total_correction_cost,
        "total_elapsed_seconds": (
            max(0.0, float(result.attempts[0].cost_seconds))
            + result.total_correction_cost
        ),
        "best_error_count": len(
            normalized_validation_error_identities(result.best.validation_errors)
        ),
        "best_valid_item_count": len(result.best.valid_item_keys),
    }


def correction_metrics_with_session_acquisition(
    metrics: dict[str, object],
    acquisition: AuthorSessionAcquisitionResult[T],
) -> dict[str, object]:
    """Add pre-author work without misclassifying it as correction work."""

    combined = dict(metrics)
    attempts = acquisition.attempts
    invocation_failures = acquisition.invocation_failures
    if acquisition.status == "acquired":
        # The acquired author turn is already the initial attempt in ``metrics``.
        pre_author_attempts = attempts[:-1]
        acquisition_cost = sum(
            max(0.0, float(attempt.cost_seconds)) for attempt in pre_author_attempts
        ) + sum(failure.cost_seconds for failure in invocation_failures)
        combined.update(
            {
                "attempt_count": int(combined.get("attempt_count") or 0)
                + len(pre_author_attempts)
                + len(invocation_failures),
                "session_acquisition_retry_count": (
                    len(pre_author_attempts) + len(invocation_failures)
                ),
                "session_acquisition_failure_count": len(invocation_failures),
                "session_acquisition_cost_seconds": acquisition_cost,
                "total_elapsed_seconds": float(
                    combined.get("total_elapsed_seconds") or 0.0
                )
                + acquisition_cost,
            }
        )
        return combined

    acquisition_cost = acquisition.total_cost
    invocation_count = len(attempts) + len(invocation_failures)
    combined.update(
        {
            "attempt_count": invocation_count,
            "correction_turn_count": 0,
            "session_acquisition_retry_count": max(0, invocation_count - 1),
            "session_acquisition_failure_count": len(invocation_failures),
            "session_acquisition_cost_seconds": acquisition_cost,
            "total_correction_cost_seconds": 0.0,
            "total_elapsed_seconds": acquisition_cost,
        }
    )
    return combined


def run_progressive_correction(
    *,
    initial: CorrectionObservation[T],
    invoke_correction: Callable[
        [CorrectionObservation[T], int, CorrectionAssessment | None],
        CorrectionObservation[T],
    ],
    pause_policy: (
        Callable[
            [CorrectionObservation[T], CorrectionAssessment | None, float, float],
            str | None,
        ]
        | None
    ) = None,
    resume_from: CorrectionRunResult[T] | None = None,
) -> CorrectionRunResult[T]:
    """Continue the exact author session until accepted or objectively unable to progress.

    There is intentionally no ordinary attempt-count or elapsed-time cap. Operational budget
    owners may pause outside this primitive, preserving ``best`` and the complete attempt chain.
    """

    if resume_from is not None:
        if not resume_from.status.startswith("repairable_paused:"):
            raise ValueError("correction_resume_status_not_repairable")
        if initial.state_sha256 != resume_from.current.state_sha256:
            raise ValueError("correction_resume_initial_frontier_mismatch")
        if initial.agent_session_id != resume_from.current.agent_session_id:
            raise ValueError("correction_resume_agent_session_mismatch")
        if initial.continuity_key != resume_from.current.continuity_key:
            raise ValueError("correction_resume_continuity_key_mismatch")
    elif not initial.validation_errors:
        return CorrectionRunResult(
            status="accepted",
            current=initial,
            best=initial,
            attempts=(initial,),
            assessments=(),
        )
    if initial.agent_session_id is None or initial.continuity_key is None:
        return CorrectionRunResult(
            status="repairable_paused:continuation_unavailable",
            current=initial,
            best=initial,
            attempts=(initial,),
            assessments=(),
        )

    if resume_from is None:
        tracker = CorrectionFrontier(initial)
        attempts: list[CorrectionObservation[T]] = [initial]
        assessments: list[CorrectionAssessment] = []
        invocation_failures: list[CorrectionInvocationFailure] = []
        correction_cost_since_progress = 0.0
        total_correction_cost = 0.0
    else:
        tracker = CorrectionFrontier.resume(
            current=resume_from.current,
            best=resume_from.best,
            attempts=resume_from.attempts,
            assessments=resume_from.assessments,
        )
        attempts = list(resume_from.attempts)
        assessments = list(resume_from.assessments)
        invocation_failures = list(resume_from.invocation_failures)
        correction_cost_since_progress = max(
            0.0, float(resume_from.correction_cost_since_progress)
        )
        total_correction_cost = max(0.0, float(resume_from.total_correction_cost))
    seen_invocation_failure_identities = {
        failure.failure_identity for failure in invocation_failures
    }
    attempt_number = len(attempts) + len(invocation_failures) + 1
    prior_assessment = assessments[-1] if assessments else None
    while True:
        invocation_started = time.monotonic()
        try:
            candidate = invoke_correction(tracker.current, attempt_number, prior_assessment)
        except Exception as exc:  # noqa: BLE001 - preserve frontier on transport failure
            failure_cost = max(0.0, time.monotonic() - invocation_started)
            total_correction_cost += failure_cost
            correction_cost_since_progress += failure_cost
            error_type = type(exc).__name__
            error_message = normalize_validation_error(str(exc))
            failure_identity = correction_state_sha256(
                candidate={
                    "phase": "exact_session_correction",
                    "agent_session_id": tracker.current.agent_session_id,
                    "error_type": error_type,
                    "error_message": error_message,
                },
                validation_errors=[f"{error_type}: {error_message}"],
            )
            failure = CorrectionInvocationFailure(
                attempt_number=attempt_number,
                agent_session_id=str(tracker.current.agent_session_id),
                error_type=error_type,
                error_message=error_message,
                failure_identity=failure_identity,
                cost_seconds=failure_cost,
            )
            invocation_failures.append(failure)
            if pause_policy is not None:
                pause_reason = pause_policy(
                    tracker.current,
                    prior_assessment,
                    correction_cost_since_progress,
                    total_correction_cost,
                )
                if pause_reason:
                    return CorrectionRunResult(
                        status="repairable_paused:" + str(pause_reason),
                        current=tracker.current,
                        best=tracker.best,
                        attempts=tuple(attempts),
                        assessments=tuple(assessments),
                        invocation_failures=tuple(invocation_failures),
                        operational_error=f"{error_type}: {error_message}",
                        correction_cost_since_progress=correction_cost_since_progress,
                        total_correction_cost=total_correction_cost,
                    )
            if failure_identity in seen_invocation_failure_identities:
                return CorrectionRunResult(
                    status="repairable_paused:correction_invocation_repeated",
                    current=tracker.current,
                    best=tracker.best,
                    attempts=tuple(attempts),
                    assessments=tuple(assessments),
                    invocation_failures=tuple(invocation_failures),
                    operational_error=f"{error_type}: {error_message}",
                    correction_cost_since_progress=correction_cost_since_progress,
                    total_correction_cost=total_correction_cost,
                )
            seen_invocation_failure_identities.add(failure_identity)
            attempt_number += 1
            continue
        attempts.append(candidate)
        candidate_cost = max(0.0, float(candidate.cost_seconds))
        total_correction_cost += candidate_cost
        assessment = tracker.assess(candidate)
        assessments.append(assessment)
        prior_assessment = assessment
        if assessment.decision == "accepted":
            return CorrectionRunResult(
                status="corrected",
                current=tracker.current,
                best=tracker.best,
                attempts=tuple(attempts),
                assessments=tuple(assessments),
                invocation_failures=tuple(invocation_failures),
                correction_cost_since_progress=0.0,
                total_correction_cost=total_correction_cost,
            )
        if assessment.decision == "continuity_failed":
            return CorrectionRunResult(
                status="repairable_paused:" + assessment.reason,
                current=tracker.current,
                best=tracker.best,
                attempts=tuple(attempts),
                assessments=tuple(assessments),
                invocation_failures=tuple(invocation_failures),
                correction_cost_since_progress=correction_cost_since_progress,
                total_correction_cost=total_correction_cost,
            )
        if assessment.decision == "stalled":
            return CorrectionRunResult(
                status="stalled:" + assessment.reason,
                current=tracker.current,
                best=tracker.best,
                attempts=tuple(attempts),
                assessments=tuple(assessments),
                invocation_failures=tuple(invocation_failures),
                correction_cost_since_progress=correction_cost_since_progress,
                total_correction_cost=total_correction_cost,
            )
        if assessment.reset_progress_clock:
            correction_cost_since_progress = 0.0
        else:
            correction_cost_since_progress += candidate_cost
        if pause_policy is not None:
            pause_reason = pause_policy(
                tracker.current,
                assessment,
                correction_cost_since_progress,
                total_correction_cost,
            )
            if pause_reason:
                return CorrectionRunResult(
                    status="repairable_paused:" + str(pause_reason),
                    current=tracker.current,
                    best=tracker.best,
                    attempts=tuple(attempts),
                    assessments=tuple(assessments),
                    invocation_failures=tuple(invocation_failures),
                    correction_cost_since_progress=correction_cost_since_progress,
                    total_correction_cost=total_correction_cost,
                )
        attempt_number += 1


__all__ = [
    "AuthorSessionAcquisitionFailure",
    "AuthorSessionAcquisitionResult",
    "CorrectionAssessment",
    "CorrectionFrontier",
    "CorrectionInvocationFailure",
    "CorrectionObservation",
    "CorrectionRunResult",
    "acquire_author_session",
    "correction_metrics_with_session_acquisition",
    "correction_state_sha256",
    "correction_run_metrics",
    "normalize_validation_error",
    "run_progressive_correction",
]
