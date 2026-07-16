# ruff: noqa: E501,F401,F403,F405
from __future__ import annotations

from collections.abc import Mapping

from backlog_core import (
    assess_research_readiness,
    priority_decision_allows_downstream,
    research_actionability_assessment,
)
from backlog_miner.prompt_correction import (
    CorrectionRunResult,
    acquire_author_session,
    correction_metrics_with_session_acquisition,
)

from usertest_backlog.shared import *
from usertest_backlog.workflows.depth_contracts import (
    assess_repo_grounding,
    parse_optioning_response,
    read_only_stage_tools,
    read_repo_revision,
    stage_include_directories,
)


def _priority_progression_blockers(
    decision: Mapping[str, Any],
    *,
    problem_record: Mapping[str, Any],
    problem_id: str,
) -> list[str]:
    blockers: list[str] = []
    if _coerce_string(decision.get("_parse_warning")) is not None:
        blockers.append("priority_parse_warning_present")
    if not priority_decision_allows_downstream(decision):
        blockers.append("priority_not_selected_for_research")
    if _coerce_string(decision.get("problem_id")) != problem_id:
        blockers.append("priority_problem_id_mismatch")
    if _coerce_string(decision.get("case_id")) != _coerce_string(
        problem_record.get("case_id")
    ):
        blockers.append("priority_case_id_mismatch")
    return blockers


def _optioning_json_value(response: str) -> Any:
    """Return strict plain or single-fenced JSON for partial item validation."""

    text = response.strip()
    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 : -3].strip()
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def _optioning_response_projection(
    response: str,
    *,
    expected_problem_id: str,
    known_family_ids: set[str],
    research_dossier: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str], list[str]]:
    """Retain valid options while exposing every deterministic envelope/item error.

    A malformed envelope must be corrected, but it does not make an independently valid
    option cease to exist.  The salvage pass validates options incrementally against the
    same causal contract, including cross-option duplicate-mechanism checks, and never
    fabricates a successful zero-option decision.
    """

    try:
        outcome, options, warnings = parse_optioning_response(
            response,
            expected_problem_id=expected_problem_id,
            known_family_ids=known_family_ids,
            research_dossier=research_dossier,
        )
        errors = [str(warning) for warning in warnings if str(warning).strip()]
        if outcome.get("optioning_status") == "invalid_output" and not errors:
            errors.append(
                f"solution_optioner_invalid_output_no_valid_options:{expected_problem_id}"
            )
        valid_keys = sorted(
            {
                "solution_option:" + str(option.get("option_id"))
                for option in options
                if _coerce_string(option.get("option_id")) is not None
            }
        )
        return outcome, options, list(dict.fromkeys(errors)), valid_keys
    except Exception as exc:  # noqa: BLE001 - exact parser feedback is repair input
        errors = [f"{type(exc).__name__}: {exc}"]

    raw = _optioning_json_value(response)
    raw_options = raw.get("options") if isinstance(raw, dict) else raw if isinstance(raw, list) else None
    valid: list[dict[str, Any]] = []
    rejected = 0
    if isinstance(raw_options, list):
        for index, candidate in enumerate(raw_options):
            if len(valid) >= 3:
                errors.append(
                    f"solution_optioner_too_many_options: expected<=3 got={len(raw_options)}"
                )
                rejected += 1
                continue
            synthetic = {
                "problem_id": expected_problem_id,
                "optioning_status": "options_produced",
                "decision_rationale": "partial item validation only",
                "options": [*valid, candidate],
            }
            try:
                _, candidate_options, candidate_warnings = parse_optioning_response(
                    json.dumps(synthetic, ensure_ascii=False),
                    expected_problem_id=expected_problem_id,
                    known_family_ids=known_family_ids,
                    research_dossier=research_dossier,
                )
            except Exception as candidate_exc:  # noqa: BLE001 - retain exact item error
                errors.append(
                    "solution_optioner_partial_option_invalid:"
                    f"index={index}:{type(candidate_exc).__name__}: {candidate_exc}"
                )
                rejected += 1
                continue
            if candidate_warnings or len(candidate_options) != len(valid) + 1:
                errors.extend(str(warning) for warning in candidate_warnings)
                rejected += 1
                continue
            valid = [dict(option) for option in candidate_options]

    rationale = (
        _coerce_string(raw.get("decision_rationale"))
        if isinstance(raw, dict)
        else None
    )
    outcome = {
        "problem_id": expected_problem_id,
        "optioning_status": "invalid_output",
        "decision_rationale": rationale or "The optioning response envelope is invalid.",
        "option_count": len(valid),
        "rejected_option_count": rejected,
    }
    valid_keys = sorted(
        {
            "solution_option:" + str(option.get("option_id"))
            for option in valid
            if _coerce_string(option.get("option_id")) is not None
        }
    )
    return outcome, valid, list(dict.fromkeys(errors)), valid_keys


def _optioning_correction_progress(assessment: Any) -> dict[str, Any] | None:
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


def _optioning_correction_prompt(
    *,
    original_prompt: str,
    prior_response: str,
    validation_errors: tuple[str, ...],
    valid_item_keys: tuple[str, ...],
    prior_assessment: Any,
) -> str:
    return (
        "SAME-AUTHOR SOLUTION-OPTIONING RESPONSE CORRECTION\n\n"
        "Revise your immediately prior complete response in this exact author session and "
        "exact repository workspace. Your prior repository reads remain content-bound to this "
        "session; use read-only tools again only when a validator error requires more evidence. "
        "Do not restart optioning or invent support. Preserve valid keyed options unless a "
        "correlated correction requires changing them. Unknown validator errors are valid "
        "feedback. Return the complete corrected JSON object, not a patch and no prose. Zero "
        "options remains valid only with an honest `insufficient_evidence` or `no_safe_option` "
        "outcome.\n\n"
        "Original assignment prompt SHA-256: "
        f"{sha256(original_prompt.encode('utf-8')).hexdigest()}\n"
        "Immediately prior response SHA-256: "
        f"{sha256(prior_response.encode('utf-8')).hexdigest()}\n\n"
        "Deterministic parse and quality errors:\n"
        + "\n".join(f"- {error}" for error in validation_errors)
        + "\n\nValid keyed options to preserve:\n"
        + ("\n".join(f"- {key}" for key in valid_item_keys) or "- none verified yet")
        + "\n\nPrior correction progress:\n"
        + json.dumps(
            _optioning_correction_progress(prior_assessment),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _optioning_attempt_history(
    correction: Any,
    *,
    base_tag: str,
    attempt_number_offset: int = 0,
) -> list[dict[str, Any]]:
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
    history.extend(
        {
            "schema_version": 2,
            "attempt_number": failure.attempt_number + attempt_number_offset,
            "attempt_tag": f"{base_tag}_correction_{failure.attempt_number - 1:03d}",
            "status": "invocation_failed",
            "agent_session_id": failure.agent_session_id,
            "resumed_from_session_id": failure.agent_session_id,
            "elapsed_seconds": failure.cost_seconds,
            "validation_errors": [f"{failure.error_type}: {failure.error_message}"],
            "failure_identity": failure.failure_identity,
        }
        for failure in correction.invocation_failures
    )
    return sorted(history, key=lambda record: int(record["attempt_number"]))


def _run_optioning_prompt_with_correction(
    *,
    stage: str,
    prompt: str,
    out_dir: Path,
    tag: str,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    workspace_dir: Path,
    expected_problem_id: str,
    repo_revision: str,
    known_family_ids: set[str],
    research_dossier: dict[str, Any],
    initial_resume_session_id: str | None = None,
    author_cost_seconds: float | None = None,
    external_feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one optional 0-3 option conversation and preserve its repair frontier."""

    import time as _time

    origin_prompt = prompt
    if external_feedback is not None:
        prompt = (
            "INDEPENDENT QUALIFICATION CORRECTION\n\n"
            "Continue this exact optioner conversation and return the complete Stage 4 "
            "response. Preserve valid mechanisms while correcting the bound independent "
            "finding. A separate adjudicator remains authoritative.\n\nBOUND FEEDBACK:\n"
            + json.dumps(external_feedback, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n\nCURRENT FULL OPTIONING INPUT AND RESPONSE CONTRACT:\n"
            + origin_prompt
        )
    prompt_sha256 = sha256(origin_prompt.encode("utf-8")).hexdigest()

    def run_attempt(
        *,
        attempt_prompt: str,
        attempt_tag: str,
        attempt_number: int,
        resume_session_id: str | None,
    ) -> CorrectionObservation[dict[str, Any]]:
        started = _time.monotonic()
        response = ""
        session_id: str | None = None
        observed_workspace = workspace_dir.resolve()
        resumed_from = resume_session_id
        prompt_path = out_dir / f"{attempt_tag}.prompt.txt"
        response_path = out_dir / f"{attempt_tag}.response.txt"
        invocation_path = out_dir / f"{attempt_tag}.model_invocation.json"
        transport_error: str | None = None
        try:
            run = run_stage_prompt_json(
                stage=stage,
                prompt=attempt_prompt,
                out_dir=out_dir,
                tag=attempt_tag,
                agent=agent,
                model=model,
                cfg=cfg,
                workspace_dir=workspace_dir,
                allowed_tools=read_only_stage_tools(agent),
                include_directories=stage_include_directories(agent, workspace_dir),
                resume_session_id=resume_session_id,
                allow_empty=True,
                structured=True,
            )
            if isinstance(run, str):
                response = run
                elapsed_seconds = max(0.0, _time.monotonic() - started)
            else:
                response = str(run.response)
                session_id = _coerce_string(run.agent_session_id)
                elapsed_seconds = max(0.0, float(run.elapsed_seconds))
                if run.workspace_dir is not None:
                    observed_workspace = Path(run.workspace_dir).resolve()
                resumed_from = _coerce_string(run.resumed_from_session_id)
                prompt_path = Path(run.prompt_path)
                response_path = Path(run.response_path)
                invocation_path = Path(run.invocation_manifest_path)
        except Exception as exc:  # noqa: BLE001 - preserve a nonblocking frontier
            if resume_session_id is not None:
                raise
            elapsed_seconds = max(0.0, _time.monotonic() - started)
            transport_error = f"{type(exc).__name__}: {exc}"

        outcome, options, errors, valid_item_keys = _optioning_response_projection(
            response,
            expected_problem_id=expected_problem_id,
            known_family_ids=known_family_ids,
            research_dossier=research_dossier,
        )
        if transport_error is not None:
            errors.insert(0, transport_error)
        errors = list(dict.fromkeys(errors))
        continuity_key = sha256(
            json.dumps(
                {
                    "workspace_dir": str(observed_workspace),
                    "repo_revision": repo_revision,
                    "original_prompt_sha256": prompt_sha256,
                    "problem_id": expected_problem_id,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        attempt_record = {
            "schema_version": 2,
            "attempt_number": attempt_number,
            "attempt_tag": attempt_tag,
            "status": "verified" if not errors else "invalid",
            "agent_session_id": session_id,
            "resumed_from_session_id": resumed_from,
            "workspace_dir": str(observed_workspace),
            "repo_revision": repo_revision,
            "elapsed_seconds": elapsed_seconds,
            "prompt_sha256": sha256(attempt_prompt.encode("utf-8")).hexdigest(),
            "response_sha256": sha256(response.encode("utf-8")).hexdigest(),
            "validation_errors": errors,
            "valid_item_keys": valid_item_keys,
            "artifacts": {
                "prompt": str(prompt_path.resolve()),
                "response": str(response_path.resolve()),
                "model_invocation": str(invocation_path.resolve()),
            },
        }
        payload = {
            "response": response,
            "outcome": outcome,
            "options": options,
            "attempt_record": attempt_record,
        }
        return CorrectionObservation(
            payload=payload,
            validation_errors=tuple(errors),
            state_sha256=correction_state_sha256(
                candidate=response,
                validation_errors=errors,
                valid_item_keys=valid_item_keys,
            ),
            valid_item_keys=tuple(valid_item_keys),
            agent_session_id=session_id,
            continuity_key=continuity_key,
            cost_seconds=elapsed_seconds,
        )

    initial = run_attempt(
        attempt_prompt=prompt,
        attempt_tag=tag,
        attempt_number=1,
        resume_session_id=initial_resume_session_id,
    )
    acquisition_attempts: tuple[CorrectionObservation[dict[str, Any]], ...] = ()
    acquisition_status = "not_required"
    if (
        initial_resume_session_id is None
        and agent.strip().lower() == "codex"
        and initial.agent_session_id is None
    ):
        acquisition = acquire_author_session(
            initial=initial,
            invoke_fresh=lambda attempt_number: run_attempt(
                attempt_prompt=prompt,
                attempt_tag=f"{tag}_session_acquisition_{attempt_number - 1:03d}",
                attempt_number=attempt_number,
                resume_session_id=None,
            ),
        )
        acquisition_status = acquisition.status
        acquisition_attempts = acquisition.attempts
        initial = acquisition.current

    def invoke_correction(
        current: CorrectionObservation[dict[str, Any]],
        attempt_number: int,
        prior_assessment: Any,
    ) -> CorrectionObservation[dict[str, Any]]:
        correction_prompt = _optioning_correction_prompt(
            original_prompt=prompt,
            prior_response=str(current.payload.get("response") or ""),
            validation_errors=current.validation_errors,
            valid_item_keys=current.valid_item_keys,
            prior_assessment=prior_assessment,
        )
        return run_attempt(
            attempt_prompt=correction_prompt,
            attempt_tag=f"{tag}_correction_{attempt_number - 1:03d}",
            attempt_number=attempt_number + max(0, len(acquisition_attempts) - 1),
            resume_session_id=initial.agent_session_id,
        )

    # Retain inherited author duration as provenance/telemetry only. Whether a model turn
    # happens to take longer than its author turn is not evidence that repair has stalled.
    _ = author_cost_seconds
    if acquisition_status.startswith("repairable_paused:"):
        correction = CorrectionRunResult(
            status=acquisition_status,
            current=initial,
            best=initial,
            attempts=acquisition_attempts,
            assessments=(),
            correction_cost_since_progress=acquisition.cost_since_progress,
            total_correction_cost=max(
                0.0,
                acquisition.total_cost
                - max(0.0, float(acquisition_attempts[0].cost_seconds)),
            ),
        )
    else:
        correction = run_progressive_correction(
            initial=initial,
            invoke_correction=invoke_correction,
        )
    retained = (
        correction.current
        if correction.status in {"accepted", "corrected"}
        else correction.best
    )
    options = [
        dict(option)
        for option in retained.payload.get("options", [])
        if isinstance(option, dict)
    ]
    outcome_raw = retained.payload.get("outcome")
    outcome = dict(outcome_raw) if isinstance(outcome_raw, dict) else {}
    if correction.status not in {"accepted", "corrected"}:
        if options and outcome.get("optioning_status") == "options_produced":
            outcome.update(
                {
                    "problem_id": expected_problem_id,
                    "optioning_status": "options_produced",
                    "decision_rationale": (
                        _coerce_string(outcome.get("decision_rationale"))
                        or "Independently valid options were retained from an incomplete "
                        "same-author correction conversation."
                    ),
                    "option_count": len(options),
                }
            )
        else:
            retained_valid_option_count = len(options)
            options = []
            outcome = {
                "problem_id": expected_problem_id,
                "optioning_status": "insufficient_evidence",
                "decision_rationale": (
                    "The same-author correction frontier did not establish a valid optioning "
                    f"decision ({correction.status}); do not infer a safe mechanism from a "
                    "malformed or contradictory envelope."
                ),
                "option_count": 0,
                "rejected_option_count": int(outcome.get("rejected_option_count") or 0),
                "retained_valid_option_count": retained_valid_option_count,
                "research_readiness_blockers": [
                    "optioning_author_correction_incomplete:" + correction.status
                ],
            }
    metrics = correction_run_metrics(correction, expected_quality=None)
    if acquisition_attempts:
        metrics = correction_metrics_with_session_acquisition(metrics, acquisition)
    history = _optioning_attempt_history(
        correction,
        base_tag=tag,
        attempt_number_offset=max(0, len(acquisition_attempts) - 1),
    )
    if acquisition_status == "acquired" and len(acquisition_attempts) > 1:
        pre_author_attempts = acquisition_attempts[:-1]
        history = [
            dict(attempt.payload["attempt_record"])
            for attempt in pre_author_attempts
        ] + history
    outcome["correction_status"] = correction.status
    outcome["correction_metrics"] = metrics
    outcome["attempt_history"] = history
    outcome["correction_cost_since_progress"] = correction.correction_cost_since_progress
    outcome["total_correction_cost"] = correction.total_correction_cost
    outcome["valid_item_keys"] = list(retained.valid_item_keys)
    if correction.operational_error is not None:
        outcome["correction_operational_error"] = correction.operational_error
    return {
        "outcome": outcome,
        "options": options,
        "warnings": list(retained.validation_errors),
        "correction_status": correction.status,
        "correction_metrics": metrics,
        "attempt_history": history,
        "correction_cost_since_progress": correction.correction_cost_since_progress,
        "total_correction_cost": correction.total_correction_cost,
        "operational_error": correction.operational_error,
    }


def _render_solution_options_markdown(
    options: list[dict[str, Any]],
    *,
    problem_records_by_id: dict[str, dict[str, Any]],
    family_order: list[str],
    family_labels_by_id: dict[str, str],
    optioning_outcomes_by_id: dict[str, dict[str, Any]] | None = None,
    title: str = "Solution Options",
) -> str:
    """Render stage-4 solution options as a human-readable Markdown document."""
    lines: list[str] = [f"# {title}\n"]
    outcomes = optioning_outcomes_by_id or {}
    if not options and not outcomes:
        lines.append("_No solution options produced._\n")
        return "\n".join(lines)

    by_problem: dict[str, list[dict[str, Any]]] = {}
    for opt in options:
        pid = opt.get("problem_id")
        if not isinstance(pid, str) or not pid.strip():
            continue
        by_problem.setdefault(pid.strip(), []).append(opt)

    family_rank = {family_id: index for index, family_id in enumerate(family_order)}
    for pid in sorted(set(by_problem) | set(outcomes)):
        rec = problem_records_by_id.get(pid) or {}
        rec_title = rec.get("title") or pid
        lines.append(f"## {rec_title}")
        lines.append(f"**Problem ID**: `{pid}`\n")

        outcome = outcomes.get(pid) or {}
        outcome_status = outcome.get("optioning_status")
        if outcome_status:
            lines.append(f"- Optioning status: `{outcome_status}`")
        outcome_rationale = outcome.get("decision_rationale")
        if isinstance(outcome_rationale, str) and outcome_rationale.strip():
            lines.append(f"- Decision rationale: {outcome_rationale.strip()}")
        if outcome_status:
            lines.append("")

        opts = sorted(
            by_problem.get(pid, []),
            key=lambda item: (
                family_rank.get(str(item.get("family_id") or ""), len(family_rank)),
                str(item.get("option_id") or ""),
            ),
        )
        if not opts:
            lines.append("_No evidence-backed option advanced._\n")
            continue

        for opt in opts:
            oid = opt.get("option_id") or "(no option_id)"
            fid = _coerce_string(opt.get("family_id"))
            if fid is None:
                lines.append(f"### Evidence-backed mechanism (`{oid}`)")
            else:
                label = family_labels_by_id.get(fid, fid)
                lines.append(f"### {label} (`{fid}`)")
            lines.append(f"- Option ID: `{oid}`")
            summary = opt.get("summary") or ""
            if summary:
                lines.append(f"- Summary: {summary}")
            cs = opt.get("change_surface_hypothesis") or ""
            if cs:
                lines.append(f"- Change surface hypothesis: `{cs}`")
            tradeoffs = opt.get("tradeoffs") or ""
            if tradeoffs:
                lines.append(f"- Tradeoffs: {tradeoffs}")
            recurrence = opt.get("recurrence_prevention") or ""
            if recurrence:
                lines.append(f"- Recurrence prevention: {recurrence}")
            tests = opt.get("test_implications") or ""
            if tests:
                lines.append(f"- Test implications: {tests}")
            rationale = opt.get("rationale") or ""
            if rationale:
                lines.append(f"- Rationale: {rationale}")
            coverage = opt.get("causal_coverage")
            if isinstance(coverage, dict):
                mechanism = coverage.get("mechanism_addressed")
                if mechanism:
                    lines.append(f"- Mechanism addressed: {mechanism}")
                residual = coverage.get("residual_recurrence_paths")
                if isinstance(residual, list) and residual:
                    lines.append("- Residual recurrence paths: " + "; ".join(map(str, residual)))
            scope = opt.get("scope_evidence")
            if isinstance(scope, dict) and scope.get("scope_level"):
                lines.append(f"- Scope evidence: `{scope.get('scope_level')}`")
            warn = opt.get("_parse_warning")
            if isinstance(warn, str) and warn.strip():
                lines.append(f"> ⚠ parse warning: {warn.strip()}")
            lines.append("")

        lines.append("")

    return "\n".join(lines)


def _run_solution_optioning_stage(
    *,
    repo_root: Path,
    target_repo_roots_by_problem: dict[str, Path] | None = None,
    atoms: list[dict[str, Any]],
    problem_records: list[dict[str, Any]],
    priority_decisions: list[dict[str, Any]],
    research_dossiers: list[dict[str, Any]],
    pipeline_manifest: PipelinePromptManifest,
    artifacts_dir: Path,
    out_json: Path,
    out_md: Path,
    agent: str,
    model: str | None,
    cfg: RunnerConfig,
    dry_run: bool,
    breadth_profile: str,
    stage_guidance_text: str,
    external_corrections_by_problem: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run stage 4 using orchestrator prompts and per-problem target workspaces."""
    import json as _json

    stage = "solution_optioning"
    stage_artifacts_dir = artifacts_dir / "solution_optioning"
    stage_artifacts_dir.mkdir(parents=True, exist_ok=True)
    invocation_tracker = ModelInvocationTracker(stage_artifacts_dir)
    live_prompt_expected_count = 0

    taxonomy = pipeline_manifest.load_taxonomy()
    families_raw = taxonomy.get("solution_families")
    families = (
        [f for f in families_raw if isinstance(f, dict)] if isinstance(families_raw, list) else []
    )
    family_order: list[str] = []
    family_labels_by_id: dict[str, str] = {}
    for fam in families:
        fid = fam.get("family_id")
        if isinstance(fid, str) and fid.strip():
            family_order.append(fid.strip())
            label = fam.get("label")
            family_labels_by_id[fid.strip()] = (
                label.strip() if isinstance(label, str) and label.strip() else fid.strip()
            )
    known_family_ids = set(family_order)

    repo_intent_path = repo_root / "configs" / "repo_intent.md"
    if not repo_intent_path.exists():
        raise FileNotFoundError(f"Missing repo intent doc: {repo_intent_path}")
    repo_intent_text = repo_intent_path.read_text(encoding="utf-8", errors="replace")
    orchestrator_head_revision = read_repo_revision(repo_root)

    records_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in problem_records
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    priority_by_id: dict[str, dict[str, Any]] = {
        str(item.get("problem_id")): item
        for item in priority_decisions
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }

    # Stable order for artifacts (deterministic).
    focus_ids = sorted(
        {
            str(d.get("problem_id"))
            for d in research_dossiers
            if isinstance(d, dict) and isinstance(d.get("problem_id"), str)
        }
    )

    template_text = pipeline_manifest.template_text(pipeline_manifest.solution_optioner_template)
    taxonomy_json = _json.dumps(taxonomy, ensure_ascii=False, indent=2)
    atoms_by_id: dict[str, dict[str, Any]] = {
        str(a.get("atom_id")): a
        for a in atoms
        if isinstance(a, dict) and isinstance(a.get("atom_id"), str)
    }
    batch_breadth = compute_batch_breadth(atoms)

    all_options: list[dict[str, Any]] = []
    optioning_outcomes: list[dict[str, Any]] = []
    optioning_correction_runs: list[dict[str, Any]] = []
    warnings_list: list[str] = []
    status: str = "ok"

    for idx, pid in enumerate(focus_ids, start=1):
        dossier = next(
            (d for d in research_dossiers if isinstance(d, dict) and d.get("problem_id") == pid),
            {},
        )
        rec = records_by_id.get(pid) or {}
        dec = priority_by_id.get(pid) or {}
        priority_blockers = _priority_progression_blockers(
            dec,
            problem_record=rec,
            problem_id=pid,
        )
        if priority_blockers:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": (
                        "The upstream priority decision is not valid for progression: "
                        + ", ".join(priority_blockers)
                    ),
                    "research_readiness_blockers": priority_blockers,
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        research_ready, research_blockers = assess_research_readiness(dossier)
        if research_ready:
            receipt_ready, receipt_blockers = verify_persisted_research_evidence(dossier)
            if not receipt_ready:
                research_ready = False
                research_blockers = [
                    f"persisted_research_evidence_invalid:{blocker}" for blocker in receipt_blockers
                ]
        if not research_ready:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": (
                        "Stage 3 research did not satisfy the evidence-readiness gate: "
                        + ", ".join(research_blockers)
                    ),
                    "research_readiness_blockers": research_blockers,
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        actionability = research_actionability_assessment(dossier)
        actionability_disposition = _coerce_string(actionability.get("disposition"))
        if actionability_disposition in {"already_addressed", "non_actionable"}:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "not_required",
                    "research_actionability_disposition": actionability_disposition,
                    "decision_rationale": _coerce_string(actionability.get("rationale"))
                    or "Stage 3 established that this case does not require a product change.",
                    "evidence_refs": list(actionability.get("evidence_refs", [])),
                    "research_readiness_blockers": [],
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        if actionability_disposition == "undetermined":
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "research_actionability_disposition": "undetermined",
                    "decision_rationale": _coerce_string(actionability.get("rationale"))
                    or "Stage 3 did not determine whether a product change is required.",
                    "research_readiness_blockers": ["research_actionability_undetermined"],
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        prompt_dossier = research_prompt_projection(dossier)
        target_repo_root = (
            target_repo_roots_by_problem.get(pid)
            if target_repo_roots_by_problem is not None
            else None
        )
        if target_repo_root is None:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": "No retained exact target workspace is available.",
                    "research_readiness_blockers": ["target_workspace_missing"],
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        research_revision = _coerce_string(dossier.get("repo_revision")) or ""
        grounded, grounding_reasons, case_repo_context = assess_repo_grounding(
            target_repo_root, research_revision
        )
        if not grounded:
            optioning_outcomes.append(
                {
                    "problem_id": pid,
                    "optioning_status": "insufficient_evidence",
                    "decision_rationale": (
                        "The read-only planning workspace cannot inspect the exact "
                        f"research revision {research_revision!r}."
                    ),
                    "research_readiness_blockers": grounding_reasons,
                    "option_count": 0,
                    "rejected_option_count": 0,
                }
            )
            continue
        evidence_ids = (
            rec.get("evidence_atom_ids") if isinstance(rec.get("evidence_atom_ids"), list) else []
        )
        evidence_ids_s = [item for item in evidence_ids if isinstance(item, str) and item.strip()]
        problem_breadth = compute_problem_breadth(evidence_ids_s, atoms_by_id)
        decision_basis = _build_decision_basis(
            problem_breadth=problem_breadth,
            batch_breadth=batch_breadth,
        )

        prompt = (
            template_text.replace("{{REPO_INTENT_MD}}", repo_intent_text)
            .replace("{{STAGE_GUIDANCE}}", stage_guidance_text)
            .replace("{{TAXONOMY_JSON}}", taxonomy_json)
            .replace(
                "{{REPO_CONTEXT_JSON}}",
                _json.dumps(case_repo_context, ensure_ascii=False, indent=2),
            )
            .replace("{{BREADTH_PROFILE}}", breadth_profile)
            .replace(
                "{{PROBLEM_BREADTH_JSON}}",
                _json.dumps(problem_breadth, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{BATCH_BREADTH_JSON}}",
                _json.dumps(batch_breadth, ensure_ascii=False, indent=2),
            )
            .replace(
                "{{STRUCTURALLY_CONSTANT_BATCH_DIMENSIONS_JSON}}",
                _json.dumps(
                    batch_breadth.get("structurally_constant_dimensions", []),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            .replace(
                "{{DECISION_BASIS_JSON}}",
                _json.dumps(decision_basis, ensure_ascii=False, indent=2),
            )
            .replace("{{PROBLEM_RECORD_JSON}}", _json.dumps(rec, ensure_ascii=False, indent=2))
            .replace("{{PRIORITY_DECISION_JSON}}", _json.dumps(dec, ensure_ascii=False, indent=2))
            .replace(
                "{{RESEARCH_DOSSIER_JSON}}",
                _json.dumps(prompt_dossier, ensure_ascii=False, indent=2),
            )
        )

        tag = f"solution_optioning_{idx:03d}"
        run_out_dir = stage_artifacts_dir / tag
        run_out_dir.mkdir(parents=True, exist_ok=True)

        options: list[dict[str, Any]] = []
        if dry_run:
            status = "dry_run_synthesized"
            (run_out_dir / f"{tag}.prompt.txt").write_text(prompt, encoding="utf-8")
            (run_out_dir / f"{tag}.response.txt").write_text(
                "[dry-run] stage-4 solution optioner prompt not executed (offline mode).\n",
                encoding="utf-8",
            )
            dry_response = {
                "problem_id": pid,
                "optioning_status": "insufficient_evidence",
                "decision_rationale": (
                    "dry_run: no research execution or repository inspection occurred"
                ),
                "options": [],
            }
            outcome, options, parse_warnings = parse_optioning_response(
                _json.dumps(dry_response, ensure_ascii=False),
                expected_problem_id=pid,
                known_family_ids=known_family_ids,
                research_dossier=dossier,
            )
            optioning_outcomes.append(outcome)
            warnings_list.extend(parse_warnings)
        else:
            live_prompt_expected_count += 1
            external_correction = (
                external_corrections_by_problem.get(pid)
                if external_corrections_by_problem is not None
                else None
            )
            optioning_run = _run_optioning_prompt_with_correction(
                stage=stage,
                prompt=prompt,
                out_dir=run_out_dir,
                tag=tag,
                agent=agent,
                model=model,
                cfg=cfg,
                workspace_dir=target_repo_root,
                expected_problem_id=pid,
                repo_revision=research_revision,
                known_family_ids=known_family_ids,
                research_dossier=dossier,
                initial_resume_session_id=(
                    _coerce_string(external_correction.get("agent_session_id"))
                    if isinstance(external_correction, Mapping)
                    else None
                ),
                author_cost_seconds=(
                    external_correction.get("original_author_cost_seconds")
                    if isinstance(external_correction, Mapping)
                    else None
                ),
                external_feedback=(
                    external_correction.get("feedback")
                    if isinstance(external_correction, Mapping)
                    and isinstance(external_correction.get("feedback"), Mapping)
                    else None
                ),
            )
            outcome = dict(optioning_run["outcome"])
            options = [
                dict(option)
                for option in optioning_run["options"]
                if isinstance(option, dict)
            ]
            optioning_outcomes.append(outcome)
            warnings_list.extend(optioning_run["warnings"])
            optioning_correction_runs.append(
                {
                    "problem_id": pid,
                    "correction_status": optioning_run["correction_status"],
                    "correction_metrics": optioning_run["correction_metrics"],
                    "attempt_history": optioning_run["attempt_history"],
                    "correction_cost_since_progress": optioning_run[
                        "correction_cost_since_progress"
                    ],
                    "total_correction_cost": optioning_run["total_correction_cost"],
                    "operational_error": optioning_run["operational_error"],
                }
            )
            if optioning_run["correction_status"] not in {"accepted", "corrected"}:
                status = "completed_with_nonblocking_fallback"
                warnings_list.append(
                    "solution_optioner_correction_incomplete:"
                    f"{pid}:{optioning_run['correction_status']}"
                )

        # Attach pid for any malformed options that forgot it.
        for opt in options:
            if opt.get("problem_id") is None:
                opt["problem_id"] = pid

        all_options.extend(options)

    stage_doc = build_stage_document(
        stage,
        all_options,
        input_meta={
            "problem_record_count": len(problem_records),
            "priority_decision_count": len(priority_decisions),
            "research_dossier_count": len(research_dossiers),
            "family_ids": family_order,
            "orchestrator_head_revision": orchestrator_head_revision,
            "orchestrator_repo_root": str(repo_root.resolve()),
            "target_workspace_count": len(
                {str(path.resolve()) for path in (target_repo_roots_by_problem or {}).values()}
            )
            if target_repo_roots_by_problem is not None
            else 0,
            "repo_access": "read_only",
            "optioning_outcomes": optioning_outcomes,
            "optioning_correction_runs": optioning_correction_runs,
            "optioning_correction_summary": {
                "run_count": len(optioning_correction_runs),
                "accepted_count": sum(
                    1
                    for run in optioning_correction_runs
                    if run.get("correction_status") in {"accepted", "corrected"}
                ),
                "repaired_count": sum(
                    1
                    for run in optioning_correction_runs
                    if run.get("correction_status") == "corrected"
                ),
                "nonblocking_fallback_count": sum(
                    1
                    for run in optioning_correction_runs
                    if run.get("correction_status") not in {"accepted", "corrected"}
                ),
                "attempt_count": sum(
                    len(run.get("attempt_history", []))
                    for run in optioning_correction_runs
                ),
                "correction_turn_count": sum(
                    max(0, len(run.get("attempt_history", [])) - 1)
                    for run in optioning_correction_runs
                ),
                "total_correction_cost_seconds": sum(
                    float(run.get("total_correction_cost") or 0.0)
                    for run in optioning_correction_runs
                ),
            },
            "dry_run": dry_run,
            "breadth_profile": breadth_profile,
            "batch_breadth": batch_breadth,
            "structurally_constant_batch_dimensions": batch_breadth.get(
                "structurally_constant_dimensions", []
            ),
            "solution_optioning_status": status,
            "solution_optioning_warnings": warnings_list,
            "post_research_relation_review": (
                "runner_verified_mechanism_identity_v2; matching researched cases "
                "are canonicalized before optioning"
            ),
            "post_research_canonical_bundle_count": sum(
                1
                for dossier in research_dossiers
                if isinstance(dossier.get("post_research_same_mechanism_bundle"), dict)
            ),
        },
        artifacts={
            "solution_options_json": str(out_json),
            "solution_options_md": str(out_md),
        },
    )
    stage_doc = attach_stage_model_invocation_contract(
        stage_doc,
        agent=agent,
        dry_run=dry_run,
        manifest_refs=invocation_tracker.collect(),
        invocation_expected=live_prompt_expected_count > 0,
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        _json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    title = out_json.stem.removesuffix(".solution_options") or "Solution Options"
    out_md.write_text(
        _render_solution_options_markdown(
            [item for item in all_options if isinstance(item, dict)],
            problem_records_by_id=records_by_id,
            family_order=family_order,
            family_labels_by_id=family_labels_by_id,
            optioning_outcomes_by_id={
                str(item["problem_id"]): item
                for item in optioning_outcomes
                if isinstance(item.get("problem_id"), str)
            },
            title=f"{title} – Solution Options",
        ),
        encoding="utf-8",
    )

    print(f"[stage4] wrote {out_json}", file=sys.stderr)
    print(f"[stage4] wrote {out_md}", file=sys.stderr)
    return stage_doc


__all__ = [name for name in globals() if not name.startswith("__")]
