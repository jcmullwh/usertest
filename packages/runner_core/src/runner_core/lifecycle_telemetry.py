from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from reporter.materialize import materialize_lifecycle_metrics
from run_artifacts.lifecycle_events import (
    LifecycleContext,
    LifecycleManifest,
    ModelUsageReceipt,
    append_lifecycle_event,
    canonical_sha256,
    load_context_from_env,
    make_lifecycle_event,
    utc_now,
    write_content_addressed_model_usage_receipt,
    write_lifecycle_manifest,
)
from token_monitoring import TokenUsage, parse_codex_invocation_usage


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = "\x00".join(str(part) for part in parts).encode("utf-8", errors="replace")
    return f"{prefix}:{sha256(encoded).hexdigest()}"


def _artifact_sha256(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _token_map(usage: TokenUsage | None) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "total_tokens": usage.total_tokens,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_output_tokens,
    }


def _event_token_map(receipt: ModelUsageReceipt) -> dict[str, int] | None:
    if receipt.usage_semantics == "unattributable":
        return None
    values = {
        "total_tokens": receipt.total_tokens,
        "input_tokens": receipt.input_tokens,
        "cached_input_tokens": receipt.cached_input_tokens,
        "uncached_input_tokens": receipt.uncached_input_tokens,
        "output_tokens": receipt.output_tokens,
        "reasoning_output_tokens": receipt.reasoning_tokens,
    }
    if any(value is None for value in values.values()):
        return None
    return {key: int(value) for key, value in values.items() if value is not None}


def _base_context(
    *,
    run_dir: Path,
    parent_case_id: str | None,
    origin_stage: str | None,
    model: str | None,
    policy: str,
    run_meta: Mapping[str, Any],
) -> tuple[LifecycleContext, bool]:
    inherited = load_context_from_env(required=False)
    runner_provenance = run_meta.get("runner_implementation")
    runner_provenance = (
        runner_provenance if isinstance(runner_provenance, Mapping) else {}
    )
    fingerprint = dict(inherited.system_fingerprint) if inherited is not None else {}
    if inherited is None:
        for key, value in {
            "code_commit": runner_provenance.get("head_commit"),
            "model": model,
            "policy": policy,
        }.items():
            if isinstance(value, str) and value.strip():
                fingerprint[key] = value.strip()

    case_id = parent_case_id or (inherited.case_id if inherited is not None else None)
    run_identity = str(run_dir.resolve())
    lifecycle_id = (
        inherited.case_lifecycle_id
        if inherited is not None and inherited.case_lifecycle_id is not None
        else (_stable_id("case-lifecycle", case_id, run_identity) if case_id else None)
    )
    cycle_id = (
        inherited.cycle_id
        if inherited is not None and inherited.cycle_id is not None
        else _stable_id("runner-cycle", run_identity)
    )
    verified_controller = bool(
        inherited is not None
        and inherited.system_fingerprint.get("controller_context_verified") == "true"
    )
    return (
        LifecycleContext(
            case_lifecycle_id=lifecycle_id,
            case_id=case_id,
            cycle_id=cycle_id,
            stage=origin_stage
            or (inherited.stage if inherited is not None else None)
            or "runner",
            work_unit_id=_stable_id("runner-work", run_identity),
            session_id=inherited.session_id if inherited is not None else None,
            shared_work_id=inherited.shared_work_id if inherited is not None else None,
            parent_action_id=(
                inherited.parent_action_id if inherited is not None else None
            ),
            system_fingerprint=fingerprint,
        ),
        verified_controller,
    )


def _actor_fields(*, verified_controller: bool) -> dict[str, str]:
    return {
        "actor_type": "controller",
        "initiator_type": "controller" if verified_controller else "unknown",
        "root_initiator_type": "controller" if verified_controller else "unknown",
        "origin": "automatic" if verified_controller else "unknown_external",
        "provenance_quality": "authoritative" if verified_controller else "unknown",
    }


def _attempts(run_dir: Path) -> list[dict[str, Any]]:
    payload = _read_json(run_dir / "agent_attempts.json")
    attempts = payload.get("attempts")
    if not isinstance(attempts, list):
        return []
    return [item for item in attempts if isinstance(item, dict)]


def _write_usage_receipt(
    *,
    run_dir: Path,
    attempt: Mapping[str, Any],
    context: LifecycleContext,
    agent: str,
    model: str | None,
    baseline: TokenUsage | None,
) -> tuple[Path, ModelUsageReceipt, TokenUsage | None]:
    raw_path_value = attempt.get("raw_events_path")
    raw_path = run_dir / str(raw_path_value or "raw_events.jsonl")
    invocation_id = context.invocation_id
    assert invocation_id is not None
    session_id = (
        str(attempt["agent_session_id"])
        if isinstance(attempt.get("agent_session_id"), str)
        else context.session_id
    )
    continued = attempt.get("continued_session") is True
    if agent == "codex":
        result = parse_codex_invocation_usage(
            raw_path,
            invocation_id=invocation_id,
            baseline_high_water=baseline if continued else None,
            session_id=session_id,
        )
        attributed = result.usage
        observed = result.observed_high_water
        semantics = result.semantics
        provider = result.provider
        baseline_usage = _token_map(result.baseline_high_water)
        observed_usage = _token_map(result.observed_high_water)
    else:
        attributed = None
        observed = None
        semantics = "unattributable"
        provider = agent
        baseline_usage = {}
        observed_usage = {}
    source_digest = _artifact_sha256(raw_path)
    recorded_at = (
        str(attempt["attempt_finished_utc"])
        if isinstance(attempt.get("attempt_finished_utc"), str)
        else (
            str(attempt["attempt_started_utc"])
            if isinstance(attempt.get("attempt_started_utc"), str)
            else "1970-01-01T00:00:00Z"
        )
    )
    receipt = ModelUsageReceipt(
        receipt_id=_stable_id("usage", invocation_id, source_digest or "missing"),
        context=context,
        provider=provider,
        model=model or agent,
        usage_semantics=semantics,
        recorded_at=recorded_at,
        invocation_started_at=(
            str(attempt["attempt_started_utc"])
            if isinstance(attempt.get("attempt_started_utc"), str)
            else None
        ),
        invocation_ended_at=(
            str(attempt["attempt_finished_utc"])
            if isinstance(attempt.get("attempt_finished_utc"), str)
            else None
        ),
        input_tokens=attributed.input_tokens if attributed is not None else None,
        cached_input_tokens=(
            attributed.cached_input_tokens if attributed is not None else None
        ),
        uncached_input_tokens=(
            attributed.uncached_input_tokens if attributed is not None else None
        ),
        output_tokens=attributed.output_tokens if attributed is not None else None,
        reasoning_tokens=(
            attributed.reasoning_output_tokens if attributed is not None else None
        ),
        total_tokens=attributed.total_tokens if attributed is not None else None,
        baseline_usage=baseline_usage,
        observed_usage=observed_usage,
        source_artifact_path=(str(raw_path) if source_digest is not None else None),
        source_artifact_sha256=source_digest,
        provenance_quality=(
            "authoritative" if semantics != "unattributable" else "unknown"
        ),
    )
    path = write_content_addressed_model_usage_receipt(
        run_dir / "model_usage_receipts", receipt
    )
    return path, receipt, observed


def write_run_lifecycle_telemetry(
    *,
    run_dir: Path,
    agent: str,
    model: str | None,
    policy: str,
    parent_case_id: str | None,
    origin_stage: str | None,
    supervisor_instruction: str | None,
) -> dict[str, Any]:
    """Derive idempotent lifecycle telemetry from one completed runner directory.

    The function is observational and safe to replay. Existing idempotency keys and
    content-addressed usage receipts prevent retries from double-counting work.
    """

    run_dir = run_dir.resolve()
    run_meta = _read_json(run_dir / "run_meta.json")
    context, verified_controller = _base_context(
        run_dir=run_dir,
        parent_case_id=parent_case_id,
        origin_stage=origin_stage,
        model=model,
        policy=policy,
        run_meta=run_meta,
    )
    events_path = run_dir / "lifecycle_events.jsonl"
    actor_fields = _actor_fields(verified_controller=verified_controller)
    started_at = run_meta.get("run_started_utc")
    ended_at = run_meta.get("run_finished_utc")
    run_key = _stable_id("runner", str(run_dir))
    append_lifecycle_event(
        events_path,
        make_lifecycle_event(
            "work.created",
            context,
            idempotency_key=f"{run_key}:opened",
            occurred_at=str(started_at) if isinstance(started_at, str) else utc_now(),
            started_at=str(started_at) if isinstance(started_at, str) else None,
            evidence_paths=(str(run_dir / "run_meta.json"),),
            attributes={"scope": "pipeline", "run_dir": str(run_dir)},
            **actor_fields,
        ),
    )

    if supervisor_instruction is not None:
        instruction_hash = sha256(supervisor_instruction.encode("utf-8")).hexdigest()
        intervention_id = _stable_id("intervention", run_key, instruction_hash)
        intervention_context = replace(
            context,
            work_unit_id=_stable_id("intervention-work", intervention_id),
        )
        append_lifecycle_event(
            events_path,
            make_lifecycle_event(
                "intervention.completed",
                intervention_context,
                idempotency_key=f"{intervention_id}:completed",
                occurred_at=str(started_at) if isinstance(started_at, str) else utc_now(),
                actor_type="supervising_agent",
                initiator_type="supervising_agent",
                root_initiator_type="supervising_agent",
                origin="supervising_agent",
                intervention_id=intervention_id,
                provenance_quality="authoritative",
                attributes={
                    "intervention_kind": "supervisor_instruction",
                    "required_for_progress": True,
                    "instruction_sha256": instruction_hash,
                    "instruction_length": len(supervisor_instruction),
                },
            ),
        )

    attempts = _attempts(run_dir)
    usage_receipt_paths: list[str] = []
    open_errors: dict[str, str] = {}
    baseline_by_session: dict[str, TokenUsage] = {}
    for position, attempt in enumerate(attempts, start=1):
        attempt_number = attempt.get("attempt")
        attempt_number = attempt_number if isinstance(attempt_number, int) else position
        invocation_id = _stable_id("invocation", run_key, attempt_number)
        session_id = (
            str(attempt["agent_session_id"])
            if isinstance(attempt.get("agent_session_id"), str)
            else None
        )
        invocation_context = replace(
            context,
            work_unit_id=_stable_id("model-work", invocation_id),
            invocation_id=invocation_id,
            session_id=session_id,
            milestone_id=f"model-attempt-{attempt_number}",
        )
        attempt_started = attempt.get("attempt_started_utc")
        attempt_ended = attempt.get("attempt_finished_utc")
        append_lifecycle_event(
            events_path,
            make_lifecycle_event(
                "model.invocation.started",
                invocation_context,
                idempotency_key=f"{invocation_id}:started",
                occurred_at=(
                    str(attempt_started) if isinstance(attempt_started, str) else utc_now()
                ),
                started_at=(
                    str(attempt_started) if isinstance(attempt_started, str) else None
                ),
                actor_type="model",
                initiator_type=actor_fields["initiator_type"],
                root_initiator_type=actor_fields["root_initiator_type"],
                origin=actor_fields["origin"],
                provenance_quality=actor_fields["provenance_quality"],
                attributes={"attempt": attempt_number, "agent": agent},
            ),
        )
        baseline = baseline_by_session.get(session_id or "")
        receipt_path, receipt, observed = _write_usage_receipt(
            run_dir=run_dir,
            attempt=attempt,
            context=invocation_context,
            agent=agent,
            model=model,
            baseline=baseline,
        )
        if session_id and observed is not None:
            baseline_by_session[session_id] = observed
        relative_receipt = str(receipt_path.relative_to(run_dir)).replace("\\", "/")
        usage_receipt_paths.append(relative_receipt)
        receipt_digest = canonical_sha256(receipt.to_dict())
        append_lifecycle_event(
            events_path,
            make_lifecycle_event(
                "model.invocation.completed",
                invocation_context,
                idempotency_key=f"{invocation_id}:completed",
                occurred_at=(
                    str(attempt_ended) if isinstance(attempt_ended, str) else utc_now()
                ),
                started_at=(
                    str(attempt_started) if isinstance(attempt_started, str) else None
                ),
                ended_at=str(attempt_ended) if isinstance(attempt_ended, str) else None,
                active_seconds=(
                    float(attempt["agent_exec_wall_seconds"])
                    if isinstance(attempt.get("agent_exec_wall_seconds"), (int, float))
                    else None
                ),
                machine_wait_seconds=(
                    max(
                        0.0,
                        float(attempt["attempt_wall_seconds"])
                        - float(attempt["agent_exec_wall_seconds"]),
                    )
                    if isinstance(attempt.get("attempt_wall_seconds"), (int, float))
                    and isinstance(attempt.get("agent_exec_wall_seconds"), (int, float))
                    else None
                ),
                actor_type="model",
                initiator_type=actor_fields["initiator_type"],
                root_initiator_type=actor_fields["root_initiator_type"],
                origin=actor_fields["origin"],
                provenance_quality=receipt.provenance_quality,
                evidence_paths=(str(receipt_path),),
                artifact_hashes={"model_usage_receipt": receipt_digest},
                attributes={
                    "attempt": attempt_number,
                    "agent": agent,
                    "exit_code": attempt.get("exit_code"),
                    "usage_receipt_path": relative_receipt,
                    "usage_semantics": receipt.usage_semantics,
                    "token_usage": _event_token_map(receipt),
                    "token_scope": (
                        "implementation"
                        if (origin_stage or "").casefold()
                        in {"implementation", "verification", "review"}
                        else "qualification"
                    ),
                    "cost_scope": "direct" if context.case_id is not None else "shared",
                    "continued_session": attempt.get("continued_session") is True,
                },
            ),
        )

        validation_errors = attempt.get("report_validation_errors")
        failed = attempt.get("exit_code") not in {None, 0} or bool(validation_errors)
        if failed:
            failure_kind = str(attempt.get("failure_subtype") or "model_output_invalid")
            cluster_id = _stable_id("error", run_key, failure_kind)
            open_errors[failure_kind] = cluster_id
            append_lifecycle_event(
                events_path,
                make_lifecycle_event(
                    "error.occurred",
                    invocation_context,
                    idempotency_key=f"{cluster_id}:occurrence:{attempt_number}",
                    occurred_at=(
                        str(attempt_ended) if isinstance(attempt_ended, str) else utc_now()
                    ),
                    actor_type="model",
                    initiator_type=actor_fields["initiator_type"],
                    root_initiator_type=actor_fields["root_initiator_type"],
                    origin=actor_fields["origin"],
                    error_cluster_id=cluster_id,
                    provenance_quality="artifact_derived",
                    attributes={
                        "error_kind": failure_kind,
                        "attempt": attempt_number,
                        "terminal": position == len(attempts),
                    },
                ),
            )
        elif open_errors:
            resolution_mode = (
                "self_healed_same_author"
                if attempt.get("continued_session") is True
                else "self_healed_controller"
            )
            for failure_kind, cluster_id in sorted(open_errors.items()):
                append_lifecycle_event(
                    events_path,
                    make_lifecycle_event(
                        "error.resolved",
                        invocation_context,
                        idempotency_key=f"{cluster_id}:resolved",
                        occurred_at=(
                            str(attempt_ended)
                            if isinstance(attempt_ended, str)
                            else utc_now()
                        ),
                        actor_type="model",
                        initiator_type=actor_fields["initiator_type"],
                        root_initiator_type=actor_fields["root_initiator_type"],
                        origin=actor_fields["origin"],
                        error_cluster_id=cluster_id,
                        provenance_quality="artifact_derived",
                        attributes={
                            "error_kind": failure_kind,
                            "resolution_mode": resolution_mode,
                            "resolution_attempt": attempt_number,
                        },
                    ),
                )
            open_errors.clear()

    if open_errors:
        for failure_kind, cluster_id in sorted(open_errors.items()):
            append_lifecycle_event(
                events_path,
                make_lifecycle_event(
                    "error.resolved",
                    context,
                    idempotency_key=f"{cluster_id}:unresolved-terminal",
                    occurred_at=str(ended_at) if isinstance(ended_at, str) else utc_now(),
                    actor_type="controller",
                    initiator_type=actor_fields["initiator_type"],
                    root_initiator_type=actor_fields["root_initiator_type"],
                    origin=actor_fields["origin"],
                    error_cluster_id=cluster_id,
                    provenance_quality="artifact_derived",
                    attributes={
                        "error_kind": failure_kind,
                        "resolution_mode": "unresolved_terminal",
                    },
                ),
            )

    report = _read_json(run_dir / "report.json")
    error = _read_json(run_dir / "error.json")
    closure_valid = bool(report) and not error
    append_lifecycle_event(
        events_path,
        make_lifecycle_event(
            "work.completed",
            context,
            idempotency_key=f"{run_key}:closed",
            occurred_at=str(ended_at) if isinstance(ended_at, str) else utc_now(),
            started_at=str(started_at) if isinstance(started_at, str) else None,
            ended_at=str(ended_at) if isinstance(ended_at, str) else None,
            evidence_paths=tuple(
                str(path)
                for path in (
                    run_dir / "run_meta.json",
                    run_dir / "report.json",
                    run_dir / "error.json",
                )
                if path.exists()
            ),
            attributes={
                "scope": "pipeline",
                "closure_valid": closure_valid,
                "status": "complete" if closure_valid else "failed_incomplete",
                "wall_clock_envelope_seconds": run_meta.get("run_wall_seconds"),
            },
            **actor_fields,
        ),
    )

    manifest_path: Path | None = None
    if context.case_lifecycle_id is not None and context.case_id is not None:
        manifest = LifecycleManifest(
            case_lifecycle_id=context.case_lifecycle_id,
            case_id=context.case_id,
            created_at=str(started_at) if isinstance(started_at, str) else utc_now(),
            updated_at=str(ended_at) if isinstance(ended_at, str) else utc_now(),
            status="active" if closure_valid else "incomplete",
            usage_receipt_paths=tuple(usage_receipt_paths),
            system_fingerprint=context.system_fingerprint,
            provenance_quality=("authoritative" if verified_controller else "unknown"),
            metadata={"run_dir": str(run_dir), "agent": agent},
        )
        manifest_path = run_dir / "lifecycle_manifest.json"
        write_lifecycle_manifest(manifest_path, manifest)

    try:
        materialize_lifecycle_metrics(
            event_sources=[events_path],
            output_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry is not an execution gate
        warnings.warn(
            f"automatic runner metrics refresh failed for {events_path}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )

    return {
        "events_path": str(events_path),
        "event_count": len(events_path.read_text(encoding="utf-8").splitlines()),
        "usage_receipt_paths": usage_receipt_paths,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        "case_lifecycle_id": context.case_lifecycle_id,
        "cycle_id": context.cycle_id,
    }


__all__ = ["write_run_lifecycle_telemetry"]
