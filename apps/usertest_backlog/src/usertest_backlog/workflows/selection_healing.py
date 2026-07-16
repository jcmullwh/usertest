"""Role-preserving self-healing for solution selection and falsification.

Each model role owns a distinct conversation frontier.  Structural errors return to the
authoring role; substantive falsifier findings cross role boundaries as content-addressed
feedback.  The coordinator has no elapsed-time or cost cutoff: exact recurrence, unavailable exact
continuation, or three consecutive materially nonadvancing cycles pause the case while retaining
the strongest frontier and complete authored history.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from backlog_core import bind_falsification_review
from backlog_core.stage_contracts import parse_selection_decisions
from backlog_miner.pipeline import run_stage_prompt_json
from backlog_miner.prompt_correction import (
    CorrectionObservation,
    CorrectionRunResult,
    acquire_author_session,
    correction_metrics_with_session_acquisition,
    correction_run_metrics,
    correction_state_sha256,
    run_progressive_correction,
)
from runner_core import RunnerConfig

from usertest_backlog.workflows.depth_contracts import (
    falsification_review_errors,
    read_only_stage_tools,
    selection_quality_errors,
    stage_include_directories,
)
from usertest_backlog.workflows.solution_options import _optioning_response_projection

RoleValidator = Callable[[str], tuple[dict[str, Any], list[str], list[str]]]
_CROSS_ROLE_NONADVANCING_LIMIT = 3


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _json_value(response: str) -> Any:
    text = response.strip()
    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 : -3].strip()
    return json.loads(text)


def _nonempty(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: Any, *, require_nonempty: bool = False) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(result) != len(value) or (require_nonempty and not result):
        return None
    return result


def _progress_payload(assessment: Any) -> dict[str, Any] | None:
    if assessment is None:
        return None
    return {
        "decision": assessment.decision,
        "reason": assessment.reason,
        "resolved_error_identities": list(assessment.resolved_error_identities),
        "introduced_error_identities": list(assessment.introduced_error_identities),
        "repeated_state": assessment.repeated_state,
        "safe_frontier_updated": assessment.safe_frontier_updated,
        "global_best_updated": assessment.global_best_updated,
    }


def _role_correction_prompt(
    *,
    role: str,
    author_origin_prompt_sha256: str,
    prior_response: str,
    validation_errors: tuple[str, ...],
    valid_item_keys: tuple[str, ...],
    prior_assessment: Any,
) -> str:
    return (
        f"SAME-AUTHOR {role.upper()} RESPONSE CORRECTION\n\n"
        "Revise your immediately prior complete response in this exact role session and "
        "workspace. Do not restart the assignment or take over another role. Preserve valid "
        "keyed items unless a correlated correction requires changing them. Unknown validator "
        "errors are valid feedback. Return the complete corrected JSON response, not a patch "
        "and no prose.\n\n"
        f"Author-origin prompt SHA-256: {author_origin_prompt_sha256}\n"
        "Immediately prior response SHA-256: "
        f"{sha256(prior_response.encode('utf-8')).hexdigest()}\n\n"
        "Deterministic parse and quality errors:\n"
        + "\n".join(f"- {error}" for error in validation_errors)
        + "\n\nValid keyed items to preserve:\n"
        + ("\n".join(f"- {key}" for key in valid_item_keys) or "- none verified yet")
        + "\n\nPrior correction progress:\n"
        + json.dumps(
            _progress_payload(prior_assessment),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _attempt_history(correction: Any) -> list[dict[str, Any]]:
    history = [dict(attempt.payload["attempt_record"]) for attempt in correction.attempts]
    for index, assessment in enumerate(correction.assessments, start=1):
        history[index]["correction_progress"] = {
            "decision": assessment.decision,
            "reason": assessment.reason,
            "before_error_count": assessment.before_error_count,
            "after_error_count": assessment.after_error_count,
            "resolved_error_identities": list(assessment.resolved_error_identities),
            "introduced_error_identities": list(assessment.introduced_error_identities),
            "repeated_state": assessment.repeated_state,
            "safe_frontier_updated": assessment.safe_frontier_updated,
            "global_best_updated": assessment.global_best_updated,
            "reset_progress_clock": assessment.reset_progress_clock,
        }
    return history


def _role_run_record(run: Mapping[str, Any]) -> dict[str, Any]:
    """Return the durable, JSON-safe projection of an in-memory role run."""

    return {
        "role": run.get("role"),
        "status": run.get("status"),
        "accepted": run.get("accepted"),
        "session_id": run.get("session_id"),
        "response_sha256": run.get("response_sha256"),
        "metrics": run.get("metrics"),
        "attempt_history": run.get("attempt_history"),
        "correction_cost_since_progress": run.get("correction_cost_since_progress"),
        "total_correction_cost": run.get("total_correction_cost"),
        "operational_error": run.get("operational_error"),
    }


def _run_role_conversation(
    *,
    role: str,
    invocation_stage: str,
    initial_prompt: str,
    author_origin_prompt_sha256: str,
    out_dir: Path,
    base_tag: str,
    workspace_dir: Path,
    repo_revision: str,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    validator: RoleValidator,
    initial_resume_session_id: str | None = None,
    author_cost_seconds: float | None = None,
    use_read_only_repo_tools: bool = True,
) -> dict[str, Any]:
    """Run and structurally repair one role response in its exact conversation."""

    out_dir.mkdir(parents=True, exist_ok=True)
    continuity_seed = {
        "role": role,
        "workspace_dir": str(workspace_dir.resolve()),
        "repo_revision": repo_revision,
        "author_origin_prompt_sha256": author_origin_prompt_sha256,
    }

    def invoke(
        *,
        prompt: str,
        tag: str,
        attempt_number: int,
        resume_session_id: str | None,
    ) -> CorrectionObservation[dict[str, Any]]:
        started = time.monotonic()
        response = ""
        session_id: str | None = None
        resumed_from = resume_session_id
        observed_workspace = workspace_dir.resolve()
        prompt_path = out_dir / f"{tag}.prompt.txt"
        response_path = out_dir / f"{tag}.response.txt"
        invocation_path = out_dir / f"{tag}.model_invocation.json"
        transport_error: str | None = None
        try:
            run = run_stage_prompt_json(
                stage=invocation_stage,
                prompt=prompt,
                out_dir=out_dir,
                tag=tag,
                agent=agent,
                model=model,
                cfg=cfg,
                workspace_dir=workspace_dir,
                allowed_tools=(read_only_stage_tools(agent) if use_read_only_repo_tools else []),
                include_directories=(
                    stage_include_directories(agent, workspace_dir)
                    if use_read_only_repo_tools
                    else []
                ),
                resume_session_id=resume_session_id,
                allow_empty=True,
                structured=True,
            )
            if isinstance(run, str):
                response = run
                elapsed_seconds = max(0.0, time.monotonic() - started)
            else:
                response = str(run.response)
                session_id = _nonempty(run.agent_session_id)
                resumed_from = _nonempty(run.resumed_from_session_id)
                elapsed_seconds = max(0.0, float(run.elapsed_seconds))
                if run.workspace_dir is not None:
                    observed_workspace = Path(run.workspace_dir).resolve()
                prompt_path = Path(run.prompt_path)
                response_path = Path(run.response_path)
                invocation_path = Path(run.invocation_manifest_path)
        except Exception as exc:  # noqa: BLE001 - retain the role frontier
            if resume_session_id is not None:
                raise
            elapsed_seconds = max(0.0, time.monotonic() - started)
            transport_error = f"{type(exc).__name__}: {exc}"

        try:
            payload, errors, valid_keys = validator(response)
        except Exception as exc:  # noqa: BLE001 - preserve an already acquired UUID
            payload = {}
            errors = [f"{type(exc).__name__}: {exc}"]
            valid_keys = []
        if transport_error is not None:
            errors.insert(0, transport_error)
        if initial_resume_session_id is not None and session_id != initial_resume_session_id:
            errors.insert(
                0,
                f"{role}_exact_author_session_mismatch:"
                f"expected={initial_resume_session_id}:actual={session_id}",
            )
        if agent.strip().lower() == "codex" and session_id is None:
            errors.insert(0, f"{role}_author_session_missing")
        errors = list(dict.fromkeys(str(error) for error in errors if str(error).strip()))
        continuity_key = _canonical_sha256(
            {**continuity_seed, "observed_workspace": str(observed_workspace)}
        )
        attempt_record = {
            "schema_version": 2,
            "role": role,
            "attempt_number": attempt_number,
            "attempt_tag": tag,
            "status": "verified" if not errors else "invalid",
            "agent_session_id": session_id,
            "resumed_from_session_id": resumed_from,
            "workspace_dir": str(observed_workspace),
            "repo_revision": repo_revision,
            "elapsed_seconds": elapsed_seconds,
            "author_origin_prompt_sha256": author_origin_prompt_sha256,
            "prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
            "response_sha256": sha256(response.encode("utf-8")).hexdigest(),
            "validation_errors": errors,
            "valid_item_keys": valid_keys,
            "artifacts": {
                "prompt": str(prompt_path.resolve()),
                "response": str(response_path.resolve()),
                "model_invocation": str(invocation_path.resolve()),
            },
        }
        payload = {
            **payload,
            "response": response,
            "attempt_record": attempt_record,
        }
        return CorrectionObservation(
            payload=payload,
            validation_errors=tuple(errors),
            state_sha256=correction_state_sha256(
                candidate=response,
                validation_errors=errors,
                valid_item_keys=valid_keys,
            ),
            valid_item_keys=tuple(sorted(set(valid_keys))),
            agent_session_id=session_id,
            continuity_key=continuity_key,
            cost_seconds=elapsed_seconds,
        )

    initial = invoke(
        prompt=initial_prompt,
        tag=base_tag,
        attempt_number=1,
        resume_session_id=initial_resume_session_id,
    )

    acquisition = None
    if (
        agent.strip().lower() == "codex"
        and initial_resume_session_id is None
        and initial.agent_session_id is None
    ):
        acquisition = acquire_author_session(
            initial=initial,
            invoke_fresh=lambda acquisition_number: invoke(
                prompt=initial_prompt,
                tag=f"{base_tag}_session_acquisition_{acquisition_number - 1:03d}",
                attempt_number=acquisition_number,
                resume_session_id=None,
            ),
        )
        initial = acquisition.current

    def acquisition_history(*, include_acquired: bool) -> list[dict[str, Any]]:
        if acquisition is None:
            return []
        observations = (
            acquisition.attempts
            if include_acquired or acquisition.status != "acquired"
            else acquisition.attempts[:-1]
        )
        records = [dict(attempt.payload["attempt_record"]) for attempt in observations]
        records.extend(
            {
                "schema_version": 2,
                "role": role,
                "attempt_number": failure.attempt_number,
                "attempt_tag": (
                    f"{base_tag}_session_acquisition_"
                    f"{failure.attempt_number - 1:03d}"
                ),
                "status": "invocation_failed",
                "agent_session_id": None,
                "resumed_from_session_id": None,
                "workspace_dir": str(workspace_dir.resolve()),
                "repo_revision": repo_revision,
                "elapsed_seconds": failure.cost_seconds,
                "validation_errors": [
                    f"{failure.error_type}: {failure.error_message}"
                ],
                "failure_identity": failure.failure_identity,
            }
            for failure in acquisition.invocation_failures
        )
        return sorted(records, key=lambda record: int(record["attempt_number"]))

    if acquisition is not None and acquisition.status != "acquired":
        paused = CorrectionRunResult(
            status=acquisition.status,
            current=acquisition.current,
            best=acquisition.best,
            attempts=acquisition.attempts,
            assessments=(),
            correction_cost_since_progress=acquisition.cost_since_progress,
            total_correction_cost=0.0,
        )
        metrics = correction_metrics_with_session_acquisition(
            correction_run_metrics(paused, expected_quality=None),
            acquisition,
        )
        retained_acquisition = acquisition.best
        return {
            "role": role,
            "status": acquisition.status,
            "accepted": False,
            "retained": retained_acquisition,
            "payload": retained_acquisition.payload,
            "session_id": None,
            "response_sha256": sha256(
                str(retained_acquisition.payload.get("response") or "").encode("utf-8")
            ).hexdigest(),
            "metrics": metrics,
            "attempt_history": acquisition_history(include_acquired=True),
            "correction_cost_since_progress": acquisition.cost_since_progress,
            "total_correction_cost": 0.0,
            "operational_error": (
                f"{acquisition.invocation_failures[-1].error_type}: "
                f"{acquisition.invocation_failures[-1].error_message}"
                if acquisition.invocation_failures
                else None
            ),
            "attempt_payloads": [
                attempt.payload for attempt in acquisition.attempts
            ],
        }

    acquisition_invocation_count = (
        len(acquisition.attempts) + len(acquisition.invocation_failures)
        if acquisition is not None
        else 1
    )

    def invoke_correction(
        current: CorrectionObservation[dict[str, Any]],
        attempt_number: int,
        prior_assessment: Any,
    ) -> CorrectionObservation[dict[str, Any]]:
        correction_prompt = _role_correction_prompt(
            role=role,
            author_origin_prompt_sha256=author_origin_prompt_sha256,
            prior_response=str(current.payload.get("response") or ""),
            validation_errors=current.validation_errors,
            valid_item_keys=current.valid_item_keys,
            prior_assessment=prior_assessment,
        )
        return invoke(
            prompt=correction_prompt,
            tag=f"{base_tag}_correction_{attempt_number - 1:03d}",
            attempt_number=attempt_number + acquisition_invocation_count - 1,
            resume_session_id=initial.agent_session_id,
        )

    # ``author_cost_seconds`` remains part of the persisted provenance contract, but it is
    # telemetry rather than a correction limit. Model latency says nothing about whether an
    # invalid output is repairable. The stage-neutral controller already stops exact state
    # recurrence and three consecutive materially nonadvancing rewrites while retaining the
    # objective-best frontier. Applying the old elapsed-cost boundary here could therefore
    # pause immediately after a 37 -> 8 -> 2 improvement merely because one later rewrite took
    # longer than the original author turn.
    _ = author_cost_seconds
    correction = run_progressive_correction(
        initial=initial,
        invoke_correction=invoke_correction,
    )
    retained = (
        correction.current if correction.status in {"accepted", "corrected"} else correction.best
    )
    metrics = correction_run_metrics(correction, expected_quality=None)
    if acquisition is not None:
        metrics = correction_metrics_with_session_acquisition(metrics, acquisition)
    correction_history = _attempt_history(correction)
    correction_history.extend(
        {
            "schema_version": 2,
            "role": role,
            "attempt_number": (
                failure.attempt_number + acquisition_invocation_count - 1
            ),
            "attempt_tag": f"{base_tag}_correction_{failure.attempt_number - 1:03d}",
            "status": "invocation_failed",
            "agent_session_id": failure.agent_session_id,
            "resumed_from_session_id": failure.agent_session_id,
            "workspace_dir": str(workspace_dir.resolve()),
            "repo_revision": repo_revision,
            "elapsed_seconds": failure.cost_seconds,
            "validation_errors": [
                f"{failure.error_type}: {failure.error_message}"
            ],
            "failure_identity": failure.failure_identity,
        }
        for failure in correction.invocation_failures
    )
    correction_history.sort(key=lambda record: int(record["attempt_number"]))
    pre_author_history = acquisition_history(include_acquired=False)
    return {
        "role": role,
        "status": correction.status,
        "accepted": correction.status in {"accepted", "corrected"},
        "retained": retained,
        "payload": retained.payload,
        "session_id": initial.agent_session_id,
        "response_sha256": sha256(
            str(retained.payload.get("response") or "").encode("utf-8")
        ).hexdigest(),
        "metrics": metrics,
        "attempt_history": [*pre_author_history, *correction_history],
        "correction_cost_since_progress": correction.correction_cost_since_progress,
        "total_correction_cost": correction.total_correction_cost,
        "operational_error": correction.operational_error,
        # In-memory candidate payloads let a stage retain verified keyed items when the
        # author conversation pauses before the complete response is valid.  Durable role
        # records intentionally omit these potentially large values; the owning stage
        # decides which partial artifacts are safe to persist or advance.
        "attempt_payloads": [
            *(
                attempt.payload
                for attempt in (
                    acquisition.attempts[:-1] if acquisition is not None else ()
                )
            ),
            *(attempt.payload for attempt in correction.attempts),
        ],
    }


def _selector_response_projection(
    response: str,
    *,
    problem_id: str,
    options_by_id: dict[str, dict[str, Any]],
    research_dossier: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Validate either one selection or an explicit request for option revision."""

    try:
        raw = _json_value(response)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "invalid", "decision": None}, [f"{type(exc).__name__}: {exc}"], []
    if isinstance(raw, dict) and raw.get("selection_status") == "option_revision_requested":
        errors: list[str] = []
        if raw.get("problem_id") != problem_id:
            errors.append(f"selector_revision_problem_id_mismatch:{problem_id}")
        if _nonempty(raw.get("revision_rationale")) is None:
            errors.append(f"selector_revision_rationale_missing:{problem_id}")
        if _string_list(raw.get("option_gaps"), require_nonempty=True) is None:
            errors.append(f"selector_revision_option_gaps_invalid:{problem_id}")
        return (
            {"kind": "option_revision_requested", "decision": dict(raw)},
            errors,
            ([] if errors else [f"selector_revision_request:{problem_id}"]),
        )

    try:
        parsed, warnings = parse_selection_decisions(response)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "invalid", "decision": None}, [f"{type(exc).__name__}: {exc}"], []
    errors = [str(warning) for warning in warnings if str(warning).strip()]
    if len(parsed) != 1:
        errors.append(f"selector_decision_count_invalid:{problem_id}:got={len(parsed)}")
    decision = dict(parsed[0]) if parsed and isinstance(parsed[0], dict) else {}
    selected_option_id = _nonempty(decision.get("selected_option_id"))
    selected_option = options_by_id.get(selected_option_id or "")
    if selected_option is not None:
        decision["selected_option"] = selected_option
    errors.extend(
        selection_quality_errors(
            decision,
            expected_problem_id=problem_id,
            options_by_id=options_by_id,
            research_dossier=research_dossier,
        )
    )
    for field in (
        "selection_rationale",
        "repo_intent_alignment",
        "why_other_options_were_not_selected",
    ):
        if _nonempty(decision.get(field)) is None:
            errors.append(f"selector_{field}_missing:{problem_id}")
    if decision.get("selection_status") != "selected":
        errors.append(f"selector_selection_status_invalid:{problem_id}")
    errors = list(dict.fromkeys(errors))
    valid_keys = (
        [f"selector_selection:{problem_id}:{selected_option_id}"]
        if not errors and selected_option_id is not None
        else []
    )
    return {"kind": "selected", "decision": decision}, errors, valid_keys


def _falsifier_response_projection(
    response: str,
    *,
    problem_id: str,
    selected_option: dict[str, Any],
    research_dossier: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    try:
        raw = _json_value(response)
    except Exception as exc:  # noqa: BLE001
        return {"review": None}, [f"{type(exc).__name__}: {exc}"], []
    if not isinstance(raw, dict):
        return {"review": None}, ["solution_falsifier_response_not_an_object"], []
    option_id = str(selected_option.get("option_id") or "")
    try:
        review = bind_falsification_review(
            raw,
            problem_id=problem_id,
            selected_option=selected_option,
            research=research_dossier,
        )
    except ValueError as exc:
        return {"review": dict(raw)}, [f"solution_falsifier_server_binding_error:{exc}"], []
    errors = falsification_review_errors(
        review,
        expected_problem_id=problem_id,
        expected_option_id=option_id,
        research_dossier=research_dossier,
        selected_option=selected_option,
    )
    errors = list(dict.fromkeys(errors))
    valid_keys = (
        [f"falsifier_review:{problem_id}:{option_id}:{review.get('verdict')}"] if not errors else []
    )
    return {"review": review}, errors, valid_keys


def _labeler_response_projection(
    response: str,
    *,
    problem_id: str,
    allowed_evidence_atom_ids: set[str],
) -> tuple[dict[str, Any], list[str], list[str]]:
    try:
        raw = _json_value(response)
    except Exception as exc:  # noqa: BLE001
        return {"label": None}, [f"{type(exc).__name__}: {exc}"], []
    if not isinstance(raw, dict):
        return {"label": None}, ["selected_solution_labeler_response_not_a_dict"], []
    errors: list[str] = []
    surface = raw.get("change_surface")
    if not isinstance(surface, dict):
        errors.append("selected_solution_labeler_change_surface_invalid")
        surface = {}
    user_visible = surface.get("user_visible")
    if not isinstance(user_visible, bool):
        errors.append("selected_solution_labeler_user_visible_invalid")
    kinds = _string_list(surface.get("kinds"), require_nonempty=True)
    if kinds is None:
        errors.append("selected_solution_labeler_kinds_invalid")
    if _nonempty(surface.get("notes")) is None:
        errors.append("selected_solution_labeler_notes_missing")
    if _nonempty(raw.get("component")) is None:
        errors.append("selected_solution_labeler_component_invalid")
    if _nonempty(raw.get("intent_risk")) is None:
        errors.append("selected_solution_labeler_intent_risk_invalid")
    confidence = raw.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        errors.append("selected_solution_labeler_confidence_invalid")
    evidence_ids = _string_list(raw.get("evidence_atom_ids_used"))
    if evidence_ids is None or any(item not in allowed_evidence_atom_ids for item in evidence_ids):
        errors.append("selected_solution_labeler_evidence_atom_ids_invalid")
    elif user_visible is True and not evidence_ids:
        errors.append("selected_solution_labeler_user_visible_evidence_missing")
    errors = list(dict.fromkeys(errors))
    return (
        {"label": dict(raw)},
        errors,
        ([] if errors else [f"selected_solution_label:{problem_id}"]),
    )


def _neutral_label(*, reason: str) -> dict[str, Any]:
    return {
        "change_surface": {
            "user_visible": False,
            "kinds": ["unknown"],
            "notes": "Server-owned neutral label; ancillary labeler did not verify a label.",
        },
        "component": "unknown",
        "intent_risk": "med",
        "confidence": 0.0,
        "evidence_atom_ids_used": [],
        "label_status": "neutral_fallback",
        "label_status_reason": reason,
    }


def _optioner_continuation_context(
    stage4_doc: Mapping[str, Any] | None,
    *,
    problem_id: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(stage4_doc, Mapping):
        return None, ["optioner_stage4_provenance_missing"]
    meta = stage4_doc.get("input_meta")
    runs = meta.get("optioning_correction_runs") if isinstance(meta, Mapping) else None
    candidates = [
        run
        for run in (runs if isinstance(runs, list) else [])
        if isinstance(run, Mapping) and run.get("problem_id") == problem_id
    ]
    if len(candidates) != 1:
        return None, [f"optioner_stage4_run_identity_invalid:{problem_id}"]
    attempts_raw = candidates[0].get("attempt_history")
    attempts = (
        [attempt for attempt in attempts_raw if isinstance(attempt, Mapping)]
        if isinstance(attempts_raw, list)
        else []
    )
    if not attempts:
        return None, [f"optioner_stage4_attempt_history_missing:{problem_id}"]
    session_id = _nonempty(attempts[0].get("agent_session_id"))
    if session_id is None or any(
        _nonempty(attempt.get("agent_session_id")) != session_id for attempt in attempts
    ):
        return None, [f"optioner_stage4_session_continuity_invalid:{problem_id}"]
    verified = [attempt for attempt in attempts if attempt.get("status") == "verified"]
    retained = verified[-1] if verified else attempts[-1]
    return (
        {
            "session_id": session_id,
            "workspace_dir": _nonempty(retained.get("workspace_dir")),
            "repo_revision": _nonempty(retained.get("repo_revision")),
            "author_origin_prompt_sha256": _nonempty(attempts[0].get("prompt_sha256")),
            "prior_response_sha256": _nonempty(retained.get("response_sha256")),
            "author_cost_seconds": max(0.0, float(attempts[0].get("elapsed_seconds") or 0.0)),
            "source_attempt_tag": retained.get("attempt_tag"),
        },
        [],
    )


def _feedback_record(
    *,
    problem_id: str,
    from_role: str,
    from_session_id: str | None,
    from_response_sha256: str,
    to_role: str,
    to_session_id: str | None,
    feedback: Mapping[str, Any],
    prompt: str,
) -> dict[str, Any]:
    content_sha256 = _canonical_sha256(feedback)
    record = {
        "problem_id": problem_id,
        "from_role": from_role,
        "from_session_id": from_session_id,
        "from_response_sha256": from_response_sha256,
        "to_role": to_role,
        "to_session_id": to_session_id,
        "feedback": dict(feedback),
        "feedback_content_sha256": content_sha256,
        "delivery_prompt_sha256": sha256(prompt.encode("utf-8")).hexdigest(),
    }
    record["feedback_id"] = "feedback:" + _canonical_sha256(record)
    return record


def _falsifier_substantive_defect_count(review: Mapping[str, Any]) -> int:
    """Count unresolved causal defects without requiring identity-set inclusion.

    A lower count is objective progress even when a reviewer names the remaining defect
    differently. Wording changes at the same count do not reset rework economics.
    """

    defects: set[str] = set()
    for finding in (
        review.get("critical_findings") if isinstance(review.get("critical_findings"), list) else []
    ):
        if isinstance(finding, Mapping):
            text = _nonempty(finding.get("finding"))
            if text is not None:
                defects.add("critical:" + text.casefold())
    dispositions = (
        review.get("material_risk_dispositions")
        if isinstance(review.get("material_risk_dispositions"), list)
        else []
    )
    disposition_by_risk: dict[str, str] = {}
    for disposition in dispositions:
        if not isinstance(disposition, Mapping):
            continue
        risk = _nonempty(disposition.get("risk"))
        if risk is not None and isinstance(disposition.get("disposition"), str):
            disposition_by_risk[risk.casefold()] = str(disposition["disposition"])
        if disposition.get("disposition") != "blocks_selection":
            continue
        if risk is not None:
            defects.add("blocking_risk:" + risk.casefold())
    for field in ("unsupported_assumptions", "residual_risks"):
        for value in review.get(field) if isinstance(review.get(field), list) else []:
            text = _nonempty(value)
            if text is None:
                continue
            disposition = disposition_by_risk.get(text.casefold())
            if disposition in {"accepted", "mitigated"}:
                continue
            defects.add(field + ":" + text.casefold())
    if review.get("verdict") != "accept":
        counterargument = _nonempty(review.get("strongest_counterargument"))
        defects.add("verdict_blocker:" + (counterargument or "unspecified").casefold())
    return len(defects)


def _run_status_outcome(
    *,
    problem_id: str,
    status: str,
    reason: str,
    role_runs: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    effective_options: list[dict[str, Any]],
    revised_options: list[dict[str, Any]],
    warnings: list[str],
    rework_cost_since_progress: float,
    total_rework_cost: float,
    consecutive_material_nonprogress: int = 0,
    retained_frontier: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "status": status,
        "decision": None,
        "outcome": {
            "problem_id": problem_id,
            "selection_status": status,
            "reasons": [reason],
            "role_runs": role_runs,
            "cross_role_feedback": feedback,
            "rework_cost_since_progress": rework_cost_since_progress,
            "total_rework_cost": total_rework_cost,
            "consecutive_material_nonprogress": consecutive_material_nonprogress,
        },
        "role_runs": role_runs,
        "feedback": feedback,
        "effective_options": effective_options,
        "revised_options": revised_options,
        "warnings": [*warnings, reason],
    }
    if retained_frontier is not None:
        result["retained_frontier"] = dict(retained_frontier)
        result["outcome"]["retained_frontier"] = dict(retained_frontier)
    return result


def run_stage5_live_case(
    *,
    problem_id: str,
    index: int,
    selector_prompt: str,
    falsifier_template: str,
    labeler_template: str,
    repo_context: dict[str, Any],
    problem_record: dict[str, Any],
    prompt_dossier: dict[str, Any],
    research_dossier: dict[str, Any],
    initial_options: list[dict[str, Any]],
    stage4_doc: Mapping[str, Any] | None,
    stage_artifacts_dir: Path,
    target_repo_root: Path,
    repo_revision: str,
    evidence_atoms_preview: list[dict[str, Any]],
    evidence_atom_ids: list[str],
    known_family_ids: set[str],
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    initial_resume_session_id: str | None = None,
    author_cost_seconds: float | None = None,
    external_feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Coordinate selector, independent falsifiers, optioner revision, and labeler."""

    effective_options = [dict(option) for option in initial_options]
    revised_options: list[dict[str, Any]] = []
    role_runs: list[dict[str, Any]] = []
    feedback_records: list[dict[str, Any]] = []
    warnings: list[str] = []
    selector_origin_hash = sha256(selector_prompt.encode("utf-8")).hexdigest()
    effective_selector_prompt = selector_prompt
    if external_feedback is not None:
        effective_selector_prompt = (
            "INDEPENDENT QUALIFICATION CORRECTION\n\n"
            "Continue this exact selector conversation and return the complete Stage 5 "
            "selector response. Preserve valid causal work while correcting the bound "
            "independent finding. Independent falsifiers will review the revision, and a "
            "separate adjudicator remains authoritative.\n\nBOUND FEEDBACK:\n"
            + json.dumps(external_feedback, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n\nCURRENT FULL SELECTION INPUT AND RESPONSE CONTRACT:\n"
            + selector_prompt
        )

    def options_index() -> dict[str, dict[str, Any]]:
        return {
            str(option["option_id"]): option
            for option in effective_options
            if isinstance(option.get("option_id"), str)
        }

    selector_run = _run_role_conversation(
        role="selector",
        invocation_stage="solution_selection",
        initial_prompt=effective_selector_prompt,
        author_origin_prompt_sha256=selector_origin_hash,
        out_dir=stage_artifacts_dir / f"solution_selection_{index:03d}",
        base_tag=f"solution_selection_{index:03d}",
        workspace_dir=target_repo_root,
        repo_revision=repo_revision,
        agent=agent,
        model=model,
        cfg=cfg,
        initial_resume_session_id=initial_resume_session_id,
        author_cost_seconds=author_cost_seconds,
        validator=lambda response: _selector_response_projection(
            response,
            problem_id=problem_id,
            options_by_id=options_index(),
            research_dossier=research_dossier,
        ),
    )
    role_runs.append(_role_run_record(selector_run))
    if not selector_run["accepted"]:
        return _run_status_outcome(
            problem_id=problem_id,
            status=selector_run["status"],
            reason="selector_correction_incomplete:" + selector_run["status"],
            role_runs=role_runs,
            feedback=feedback_records,
            effective_options=effective_options,
            revised_options=revised_options,
            warnings=warnings,
            rework_cost_since_progress=0.0,
            total_rework_cost=0.0,
        )
    selector_session_id = selector_run["session_id"]
    origin_optioner_context, _origin_optioner_errors = _optioner_continuation_context(
        stage4_doc,
        problem_id=problem_id,
    )
    origin_optioner_session_id = (
        _nonempty(origin_optioner_context.get("session_id"))
        if isinstance(origin_optioner_context, Mapping)
        else None
    )
    optioner_context_state = (
        dict(origin_optioner_context) if isinstance(origin_optioner_context, Mapping) else None
    )
    if selector_session_id is not None and selector_session_id == origin_optioner_session_id:
        return _run_status_outcome(
            problem_id=problem_id,
            status="repairable_paused:role_session_collision",
            reason="optioner_and_selector_session_ids_match",
            role_runs=role_runs,
            feedback=feedback_records,
            effective_options=effective_options,
            revised_options=revised_options,
            warnings=warnings,
            rework_cost_since_progress=0.0,
            total_rework_cost=0.0,
        )
    selector_origin_cost = float(selector_run["metrics"].get("initial_cost_seconds") or 0.0)
    selector_payload = selector_run["payload"]
    selector_seen_states = {
        _canonical_sha256(
            {
                "response_sha256": selector_run["response_sha256"],
                "options_sha256": _canonical_sha256(effective_options),
            }
        )
    }
    falsifier_sessions: set[str] = set()
    reviewed_cycle_states: set[str] = set()
    falsifier_count = 0
    option_revision_count = 0
    selector_rework_count = 0
    prior_falsifier_defect_count: int | None = None
    rework_cost_since_progress = 0.0
    total_rework_cost = 0.0
    consecutive_material_nonprogress = 0
    pending_selector_progress: bool | None = None
    retained_best_frontier: dict[str, Any] = {
        "selector_response_sha256": selector_run["response_sha256"],
        "selected_option_id": None,
        "options_sha256": _canonical_sha256(effective_options),
        "falsifier_defect_count": None,
    }

    def add_rework_cost(run: Mapping[str, Any], *, progress: bool) -> None:
        nonlocal rework_cost_since_progress, total_rework_cost
        cost = float(run.get("metrics", {}).get("total_elapsed_seconds") or 0.0)
        total_rework_cost += cost
        if progress:
            rework_cost_since_progress = 0.0
        else:
            rework_cost_since_progress += cost

    def record_material_progress(progress: bool) -> int:
        nonlocal consecutive_material_nonprogress
        if progress:
            consecutive_material_nonprogress = 0
        else:
            consecutive_material_nonprogress += 1
        return consecutive_material_nonprogress

    while True:
        selector_kind = selector_payload.get("kind")
        selector_decision_raw = selector_payload.get("decision")
        selector_decision = (
            dict(selector_decision_raw) if isinstance(selector_decision_raw, dict) else {}
        )

        if selector_kind == "option_revision_requested":
            optioner_context = optioner_context_state
            optioner_errors = [] if optioner_context is not None else list(_origin_optioner_errors)
            if optioner_context is None:
                return _run_status_outcome(
                    problem_id=problem_id,
                    status="repairable_paused:optioner_continuation_unavailable",
                    reason=",".join(optioner_errors),
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            context_workspace = _nonempty(optioner_context.get("workspace_dir"))
            context_revision = _nonempty(optioner_context.get("repo_revision"))
            if (
                context_workspace is None
                or Path(context_workspace).resolve() != target_repo_root.resolve()
                or context_revision != repo_revision
            ):
                return _run_status_outcome(
                    problem_id=problem_id,
                    status="repairable_paused:optioner_workspace_continuity_failed",
                    reason=(
                        "optioner_workspace_or_revision_mismatch:"
                        f"workspace={context_workspace}:revision={context_revision}"
                    ),
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            optioner_session_id = str(optioner_context["session_id"])
            if optioner_session_id == selector_session_id:
                return _run_status_outcome(
                    problem_id=problem_id,
                    status="repairable_paused:role_session_collision",
                    reason="optioner_and_selector_session_ids_match",
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            option_revision_count += 1
            critique = {
                "revision_rationale": selector_decision.get("revision_rationale"),
                "option_gaps": selector_decision.get("option_gaps"),
                "selector_response_sha256": selector_run["response_sha256"],
                "current_option_ids": sorted(options_index()),
                "current_options_sha256": _canonical_sha256(effective_options),
            }
            optioner_origin_prompt_sha256 = optioner_context.get("author_origin_prompt_sha256")
            optioner_prior_response_sha256 = optioner_context.get("prior_response_sha256")
            optioner_prompt = (
                "ORIGINAL OPTIONER ROLE REVISION REQUEST\n\n"
                "Continue your exact Stage 4 optioning session. The original selector found "
                "the existing options causally inadequate. Use the bound critique below; do "
                "not restart research or invent evidence. Return the complete Stage 4 envelope "
                "with zero to three revised options. Honest insufficient_evidence and "
                "no_safe_option remain valid.\n\n"
                f"Original optioner prompt SHA-256: {optioner_origin_prompt_sha256}\n"
                f"Prior optioner response SHA-256: {optioner_prior_response_sha256}\n\n"
                "Bound selector critique:\n"
                + json.dumps(critique, ensure_ascii=False, indent=2, sort_keys=True)
            )
            feedback_records.append(
                _feedback_record(
                    problem_id=problem_id,
                    from_role="selector",
                    from_session_id=selector_session_id,
                    from_response_sha256=selector_run["response_sha256"],
                    to_role="optioner",
                    to_session_id=optioner_session_id,
                    feedback=critique,
                    prompt=optioner_prompt,
                )
            )
            optioner_run = _run_role_conversation(
                role="optioner",
                invocation_stage="solution_optioning",
                initial_prompt=optioner_prompt,
                author_origin_prompt_sha256=str(
                    optioner_context.get("author_origin_prompt_sha256") or ""
                ),
                out_dir=(
                    stage_artifacts_dir
                    / f"solution_option_revision_{index:03d}_{option_revision_count:03d}"
                ),
                base_tag=(f"solution_option_revision_{index:03d}_{option_revision_count:03d}"),
                workspace_dir=target_repo_root,
                repo_revision=repo_revision,
                agent=agent,
                model=model,
                cfg=cfg,
                initial_resume_session_id=optioner_session_id,
                author_cost_seconds=float(optioner_context.get("author_cost_seconds") or 0.0),
                validator=lambda response: (
                    lambda outcome, options, errors, valid: (
                        {"outcome": outcome, "options": options},
                        errors,
                        valid,
                    )
                )(
                    *_optioning_response_projection(
                        response,
                        expected_problem_id=problem_id,
                        known_family_ids=known_family_ids,
                        research_dossier=research_dossier,
                    )
                ),
            )
            role_runs.append(_role_run_record(optioner_run))
            optioner_context_state["prior_response_sha256"] = optioner_run["response_sha256"]
            add_rework_cost(optioner_run, progress=False)
            optioner_outcome = optioner_run["payload"].get("outcome")
            optioner_options = optioner_run["payload"].get("options")
            partial_valid_options = bool(
                isinstance(optioner_outcome, dict)
                and optioner_outcome.get("optioning_status") == "options_produced"
                and isinstance(optioner_options, list)
                and any(isinstance(option, dict) for option in optioner_options)
            )
            if not optioner_run["accepted"] and not partial_valid_options:
                return _run_status_outcome(
                    problem_id=problem_id,
                    status=optioner_run["status"],
                    reason="optioner_revision_incomplete:" + optioner_run["status"],
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            if not optioner_run["accepted"]:
                warnings.append(
                    "optioner_revision_partial_valid_options_retained:" + optioner_run["status"]
                )
            if not isinstance(optioner_outcome, dict):
                optioner_outcome = {}
            if optioner_outcome.get("optioning_status") != "options_produced":
                return _run_status_outcome(
                    problem_id=problem_id,
                    status=str(optioner_outcome.get("optioning_status") or "insufficient_evidence"),
                    reason=str(
                        optioner_outcome.get("decision_rationale")
                        or "Optioner produced no safe revised option."
                    ),
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            new_options = (
                [dict(option) for option in optioner_options if isinstance(option, dict)]
                if isinstance(optioner_options, list)
                else []
            )
            old_options_hash = _canonical_sha256(effective_options)
            new_options_hash = _canonical_sha256(new_options)
            effective_options = new_options
            revised_options = [dict(option) for option in new_options]
            if old_options_hash != new_options_hash:
                rework_cost_since_progress = 0.0

            selector_rework_count += 1
            selector_prompt = (
                "ORIGINAL SELECTOR ROLE OPTION-REVISION REVIEW\n\n"
                "Continue your exact selector session. The original optioner revised the option "
                "set in response to your bound critique. Select one revised option or explicitly "
                "request another revision. Do not invent or combine options. Return the complete "
                "selector response.\n\n"
                f"Optioner response SHA-256: {optioner_run['response_sha256']}\n"
                "Revised options:\n" + json.dumps(effective_options, ensure_ascii=False, indent=2)
            )
            feedback_records.append(
                _feedback_record(
                    problem_id=problem_id,
                    from_role="optioner",
                    from_session_id=optioner_session_id,
                    from_response_sha256=optioner_run["response_sha256"],
                    to_role="selector",
                    to_session_id=selector_session_id,
                    feedback={
                        "optioner_response_sha256": optioner_run["response_sha256"],
                        "revised_option_ids": sorted(options_index()),
                        "revised_options_sha256": new_options_hash,
                    },
                    prompt=selector_prompt,
                )
            )
            previous_option_id = _nonempty(selector_decision.get("selected_option_id"))
            selector_run = _run_role_conversation(
                role="selector",
                invocation_stage="solution_selection",
                initial_prompt=selector_prompt,
                author_origin_prompt_sha256=selector_origin_hash,
                out_dir=stage_artifacts_dir / f"solution_selection_{index:03d}",
                base_tag=f"solution_selection_{index:03d}_rework_{selector_rework_count:03d}",
                workspace_dir=target_repo_root,
                repo_revision=repo_revision,
                agent=agent,
                model=model,
                cfg=cfg,
                initial_resume_session_id=selector_session_id,
                author_cost_seconds=selector_origin_cost,
                validator=lambda response: _selector_response_projection(
                    response,
                    problem_id=problem_id,
                    options_by_id=options_index(),
                    research_dossier=research_dossier,
                ),
            )
            role_runs.append(_role_run_record(selector_run))
            if not selector_run["accepted"]:
                return _run_status_outcome(
                    problem_id=problem_id,
                    status=selector_run["status"],
                    reason="selector_option_revision_review_incomplete:" + selector_run["status"],
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            selector_payload = selector_run["payload"]
            new_decision = selector_payload.get("decision")
            new_option_id = (
                _nonempty(new_decision.get("selected_option_id"))
                if isinstance(new_decision, dict)
                else None
            )
            revision_progress = bool(
                old_options_hash != new_options_hash or new_option_id != previous_option_id
            )
            add_rework_cost(selector_run, progress=revision_progress)
            selector_state = _canonical_sha256(
                {
                    "response_sha256": selector_run["response_sha256"],
                    "options_sha256": new_options_hash,
                }
            )
            if selector_state in selector_seen_states:
                return _run_status_outcome(
                    problem_id=problem_id,
                    status="stalled:selector_state_recurred",
                    reason="selector_option_revision_state_recurred",
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            selector_seen_states.add(selector_state)
            if selector_payload.get("kind") == "option_revision_requested":
                record_material_progress(revision_progress)
                pending_selector_progress = None
            else:
                pending_selector_progress = revision_progress
            if (
                selector_payload.get("kind") == "option_revision_requested"
                and consecutive_material_nonprogress >= _CROSS_ROLE_NONADVANCING_LIMIT
            ):
                return _run_status_outcome(
                    problem_id=problem_id,
                    status="repairable_paused:consecutive_nonadvancing_corrections_require_adjudication",
                    reason="consecutive_nonadvancing_corrections_require_adjudication",
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                    consecutive_material_nonprogress=consecutive_material_nonprogress,
                    retained_frontier=retained_best_frontier,
                )
            continue

        if selector_kind != "selected":
            return _run_status_outcome(
                problem_id=problem_id,
                status="repairable_paused:selector_state_invalid",
                reason=f"selector_kind_invalid:{selector_kind}",
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
            )
        selected_option_id = _nonempty(selector_decision.get("selected_option_id"))
        selected_option = options_index().get(selected_option_id or "")
        if selected_option is None:
            return _run_status_outcome(
                problem_id=problem_id,
                status="repairable_paused:selector_option_missing",
                reason=f"selector_option_missing:{selected_option_id}",
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
            )

        falsifier_count += 1
        falsifier_tag = (
            f"solution_falsification_{index:03d}"
            if falsifier_count == 1
            else f"solution_falsification_{index:03d}_review_{falsifier_count:03d}"
        )
        falsifier_prompt = (
            falsifier_template.replace(
                "{{REPO_CONTEXT_JSON}}",
                json.dumps(repo_context, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{PROBLEM_RECORD_JSON}}",
                json.dumps(problem_record, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{RESEARCH_DOSSIER_JSON}}",
                json.dumps(prompt_dossier, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{SOLUTION_OPTIONS_JSON}}",
                json.dumps(effective_options, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{SELECTION_DECISION_JSON}}",
                json.dumps(selector_decision, ensure_ascii=False, indent=2),
            )
        )
        falsifier_run = _run_role_conversation(
            role="falsifier",
            invocation_stage="solution_falsification",
            initial_prompt=falsifier_prompt,
            author_origin_prompt_sha256=sha256(falsifier_prompt.encode("utf-8")).hexdigest(),
            out_dir=stage_artifacts_dir / falsifier_tag,
            base_tag=falsifier_tag,
            workspace_dir=target_repo_root,
            repo_revision=repo_revision,
            agent=agent,
            model=model,
            cfg=cfg,
            validator=lambda response, selected_option=selected_option: (
                _falsifier_response_projection(
                    response,
                    problem_id=problem_id,
                    selected_option=selected_option,
                    research_dossier=research_dossier,
                )
            ),
        )
        role_runs.append(_role_run_record(falsifier_run))
        falsifier_session_id = falsifier_run["session_id"]
        if isinstance(falsifier_session_id, str):
            if falsifier_session_id in falsifier_sessions or falsifier_session_id in {
                selector_session_id,
                origin_optioner_session_id,
            }:
                return _run_status_outcome(
                    problem_id=problem_id,
                    status="repairable_paused:falsifier_independence_failed",
                    reason="fresh_falsifier_reused_role_session",
                    role_runs=role_runs,
                    feedback=feedback_records,
                    effective_options=effective_options,
                    revised_options=revised_options,
                    warnings=warnings,
                    rework_cost_since_progress=rework_cost_since_progress,
                    total_rework_cost=total_rework_cost,
                )
            falsifier_sessions.add(falsifier_session_id)
        feedback_records.append(
            _feedback_record(
                problem_id=problem_id,
                from_role="selector",
                from_session_id=selector_session_id,
                from_response_sha256=selector_run["response_sha256"],
                to_role="falsifier",
                to_session_id=falsifier_session_id,
                feedback={
                    "selected_option_id": selected_option_id,
                    "selector_response_sha256": selector_run["response_sha256"],
                    "options_sha256": _canonical_sha256(effective_options),
                },
                prompt=falsifier_prompt,
            )
        )
        if falsifier_count > 1:
            add_rework_cost(falsifier_run, progress=False)
        if not falsifier_run["accepted"]:
            return _run_status_outcome(
                problem_id=problem_id,
                status=falsifier_run["status"],
                reason="falsifier_correction_incomplete:" + falsifier_run["status"],
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
            )
        review = falsifier_run["payload"].get("review")
        if not isinstance(review, dict):
            return _run_status_outcome(
                problem_id=problem_id,
                status="repairable_paused:falsifier_review_missing",
                reason="falsifier_review_missing_after_validation",
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
            )
        current_falsifier_defect_count = _falsifier_substantive_defect_count(review)
        falsifier_defect_progress = bool(
            prior_falsifier_defect_count is not None
            and current_falsifier_defect_count < prior_falsifier_defect_count
        )
        prior_falsifier_defect_count = current_falsifier_defect_count
        if retained_best_frontier["falsifier_defect_count"] is None or (
            current_falsifier_defect_count
            < int(retained_best_frontier["falsifier_defect_count"])
        ):
            retained_best_frontier = {
                "selector_response_sha256": selector_run["response_sha256"],
                "selected_option_id": selected_option_id,
                "options_sha256": _canonical_sha256(effective_options),
                "falsifier_defect_count": current_falsifier_defect_count,
            }
        if prior_falsifier_defect_count is not None and falsifier_count > 1:
            cycle_material_progress = bool(pending_selector_progress) or falsifier_defect_progress
            record_material_progress(cycle_material_progress)
            if cycle_material_progress:
                rework_cost_since_progress = 0.0
        pending_selector_progress = None
        if falsifier_defect_progress:
            rework_cost_since_progress = 0.0
        cycle_state = _canonical_sha256(
            {
                "options_sha256": _canonical_sha256(effective_options),
                "selector_response_sha256": selector_run["response_sha256"],
                "falsifier_response_sha256": falsifier_run["response_sha256"],
                "selected_option_id": selected_option_id,
            }
        )
        if cycle_state in reviewed_cycle_states:
            return _run_status_outcome(
                problem_id=problem_id,
                status="stalled:selection_falsification_cycle_recurred",
                reason="selection_falsification_cycle_recurred",
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
            )
        reviewed_cycle_states.add(cycle_state)

        if review.get("verdict") == "accept":
            selected_decision = dict(selector_decision)
            selected_decision["selected_option"] = selected_option
            selected_decision["falsification_review"] = review
            complete_errors = selection_quality_errors(
                selected_decision,
                expected_problem_id=problem_id,
                options_by_id=options_index(),
                research_dossier=research_dossier,
                # The independent labeler owns change-surface metadata. Validate the
                # selector/falsifier decision now and run the complete gate after labeling.
                require_complete=False,
            )
            if complete_errors:
                review = {
                    **review,
                    "verdict": "reject",
                    "strongest_counterargument": (
                        "Runner complete-selection gate rejected the provisional acceptance: "
                        + "; ".join(complete_errors)
                    ),
                }
            else:
                selected_payload = {
                    "problem_id": problem_id,
                    "title": problem_record.get("title") or problem_id,
                    "problem": problem_record.get("problem") or "",
                    "user_impact": problem_record.get("user_impact") or "",
                    "selected_option_id": selected_decision.get("selected_option_id"),
                    "selected_family_id": selected_decision.get("selected_family_id"),
                    "selection_rationale": selected_decision.get("selection_rationale"),
                    "selected_option": selected_option,
                }
                labeler_prompt = labeler_template.replace(
                    "{{SELECTED_SOLUTION_JSON}}",
                    json.dumps(selected_payload, ensure_ascii=False, indent=2),
                ).replace(
                    "{{EVIDENCE_ATOMS_JSON}}",
                    json.dumps(evidence_atoms_preview, ensure_ascii=False, indent=2),
                )
                labeler_tag = f"selected_solution_labeler_{index:03d}"
                labeler_workspace = stage_artifacts_dir / labeler_tag / "workspace"
                labeler_workspace.mkdir(parents=True, exist_ok=True)
                labeler_run = _run_role_conversation(
                    role="labeler",
                    invocation_stage="selected_solution_labeler",
                    initial_prompt=labeler_prompt,
                    author_origin_prompt_sha256=sha256(labeler_prompt.encode("utf-8")).hexdigest(),
                    out_dir=stage_artifacts_dir / labeler_tag,
                    base_tag=labeler_tag,
                    workspace_dir=labeler_workspace,
                    repo_revision=repo_revision,
                    agent=agent,
                    model=model,
                    cfg=cfg,
                    validator=lambda response: _labeler_response_projection(
                        response,
                        problem_id=problem_id,
                        allowed_evidence_atom_ids=set(evidence_atom_ids),
                    ),
                    use_read_only_repo_tools=False,
                )
                role_runs.append(_role_run_record(labeler_run))
                labeler_session_id = labeler_run.get("session_id")
                labeler_session_collision = bool(
                    isinstance(labeler_session_id, str)
                    and labeler_session_id
                    in {
                        selector_session_id,
                        origin_optioner_session_id,
                        *falsifier_sessions,
                    }
                )
                label = (
                    labeler_run["payload"].get("label")
                    if labeler_run["accepted"] and not labeler_session_collision
                    else _neutral_label(
                        reason=(
                            "labeler_role_session_collision"
                            if labeler_session_collision
                            else "labeler_correction_incomplete:" + labeler_run["status"]
                        )
                    )
                )
                if not isinstance(label, dict):
                    label = _neutral_label(reason="labeler_verified_payload_missing")
                for key in ("change_surface", "component", "intent_risk"):
                    selected_decision[key] = label.get(key)
                selected_decision["labeler_confidence"] = label.get("confidence")
                selected_decision["evidence_atom_ids_used"] = label.get("evidence_atom_ids_used")
                selected_decision["selected_solution_label_status"] = label.get(
                    "label_status", "verified"
                )
                complete_errors = selection_quality_errors(
                    selected_decision,
                    expected_problem_id=problem_id,
                    options_by_id=options_index(),
                    research_dossier=research_dossier,
                    require_complete=True,
                )
                if complete_errors:
                    review = {
                        **review,
                        "verdict": "reject",
                        "strongest_counterargument": (
                            "Runner complete-selection gate rejected the labeled acceptance: "
                            + "; ".join(complete_errors)
                        ),
                    }
                else:
                    selected_decision["role_healing"] = {
                        "role_runs": role_runs,
                        "cross_role_feedback": feedback_records,
                        "rework_cost_since_progress": rework_cost_since_progress,
                        "total_rework_cost": total_rework_cost,
                        "consecutive_material_nonprogress": consecutive_material_nonprogress,
                        "retained_best_frontier": retained_best_frontier,
                    }
                    return {
                        "status": "selected",
                        "decision": selected_decision,
                        "outcome": {
                            "problem_id": problem_id,
                            "selection_status": "selected",
                            "selected_option_id": selected_option_id,
                            "falsification_verdict": "accept",
                            "label_status": selected_decision[
                                "selected_solution_label_status"
                            ],
                            "role_runs": role_runs,
                            "cross_role_feedback": feedback_records,
                            "rework_cost_since_progress": rework_cost_since_progress,
                            "total_rework_cost": total_rework_cost,
                            "consecutive_material_nonprogress": (
                                consecutive_material_nonprogress
                            ),
                            "retained_best_frontier": retained_best_frontier,
                        },
                        "role_runs": role_runs,
                        "feedback": feedback_records,
                        "effective_options": effective_options,
                        "revised_options": revised_options,
                        "warnings": warnings,
                    }

        critique = {
            "selected_option_id": selected_option_id,
            "verdict": review.get("verdict"),
            "strongest_counterargument": review.get("strongest_counterargument"),
            "critical_findings": review.get("critical_findings"),
            "unsupported_assumptions": review.get("unsupported_assumptions"),
            "residual_risks": review.get("residual_risks"),
            "material_risk_dispositions": review.get("material_risk_dispositions"),
            "evidence_refs": review.get("evidence_refs"),
            "falsifier_response_sha256": falsifier_run["response_sha256"],
        }
        selector_rework_count += 1
        selector_prompt = (
            "ORIGINAL SELECTOR ROLE FALSIFICATION REWORK\n\n"
            "Continue your exact selector session. A fresh independent falsifier rejected or "
            "could not support the provisional selection. Address the bound critique "
            "substantively: choose another existing option, revise the selection if the same "
            "option can genuinely answer the evidence, or return an explicit "
            "option_revision_requested object. Do not approve unresolved critical findings.\n\n"
            "Bound falsifier critique:\n"
            + json.dumps(critique, ensure_ascii=False, indent=2, sort_keys=True)
        )
        feedback_records.append(
            _feedback_record(
                problem_id=problem_id,
                from_role="falsifier",
                from_session_id=falsifier_session_id,
                from_response_sha256=falsifier_run["response_sha256"],
                to_role="selector",
                to_session_id=selector_session_id,
                feedback=critique,
                prompt=selector_prompt,
            )
        )
        if consecutive_material_nonprogress >= _CROSS_ROLE_NONADVANCING_LIMIT:
            return _run_status_outcome(
                problem_id=problem_id,
                status="repairable_paused:consecutive_nonadvancing_corrections_require_adjudication",
                reason="consecutive_nonadvancing_corrections_require_adjudication",
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
                consecutive_material_nonprogress=consecutive_material_nonprogress,
                retained_frontier=retained_best_frontier,
            )
        old_selected_option_id = selected_option_id
        selector_run = _run_role_conversation(
            role="selector",
            invocation_stage="solution_selection",
            initial_prompt=selector_prompt,
            author_origin_prompt_sha256=selector_origin_hash,
            out_dir=stage_artifacts_dir / f"solution_selection_{index:03d}",
            base_tag=f"solution_selection_{index:03d}_rework_{selector_rework_count:03d}",
            workspace_dir=target_repo_root,
            repo_revision=repo_revision,
            agent=agent,
            model=model,
            cfg=cfg,
            initial_resume_session_id=selector_session_id,
            author_cost_seconds=selector_origin_cost,
            validator=lambda response: _selector_response_projection(
                response,
                problem_id=problem_id,
                options_by_id=options_index(),
                research_dossier=research_dossier,
            ),
        )
        role_runs.append(_role_run_record(selector_run))
        if not selector_run["accepted"]:
            return _run_status_outcome(
                problem_id=problem_id,
                status=selector_run["status"],
                reason="selector_falsifier_rework_incomplete:" + selector_run["status"],
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
            )
        selector_payload = selector_run["payload"]
        new_decision = selector_payload.get("decision")
        new_option_id = (
            _nonempty(new_decision.get("selected_option_id"))
            if isinstance(new_decision, dict)
            else None
        )
        progress = new_option_id is not None and new_option_id != old_selected_option_id
        add_rework_cost(selector_run, progress=progress)
        pending_selector_progress = progress
        selector_state = _canonical_sha256(
            {
                "response_sha256": selector_run["response_sha256"],
                "options_sha256": _canonical_sha256(effective_options),
            }
        )
        if selector_state in selector_seen_states:
            return _run_status_outcome(
                problem_id=problem_id,
                status="stalled:selector_state_recurred",
                reason="selector_falsifier_rework_state_recurred",
                role_runs=role_runs,
                feedback=feedback_records,
                effective_options=effective_options,
                revised_options=revised_options,
                warnings=warnings,
                rework_cost_since_progress=rework_cost_since_progress,
                total_rework_cost=total_rework_cost,
            )
        selector_seen_states.add(selector_state)


__all__ = [name for name in globals() if not name.startswith("__")]
