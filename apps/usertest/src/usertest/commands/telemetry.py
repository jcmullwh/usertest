from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import warnings
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reporter.materialize import (
    discover_lifecycle_event_logs,
    materialize_lifecycle_metrics,
)
from run_artifacts.lifecycle_events import (
    ACTION_FAMILIES,
    LifecycleContext,
    append_lifecycle_event,
    command_family,
    fingerprint_command,
    lifecycle_context_env,
    load_context_from_env,
    make_lifecycle_event,
    read_lifecycle_events,
    redact_command,
    utc_now,
)


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--events", required=True, type=Path, help="Lifecycle JSONL path.")
    parser.add_argument("--case-lifecycle-id")
    parser.add_argument("--case-id")
    parser.add_argument("--cycle-id")
    parser.add_argument("--stage")
    parser.add_argument("--milestone-id")
    parser.add_argument(
        "--work-unit-id",
        help="Unique concrete cost-unit identity; do not reuse it for another action.",
    )
    parser.add_argument("--shared-work-id")
    parser.add_argument("--beneficiary-case-lifecycle-id", action="append", default=[])
    parser.add_argument(
        "--dependency-work-unit-id",
        "--dependency-lifecycle-id",
        dest="dependency_work_unit_id",
        action="append",
        default=[],
        help="Required prior/shared work unit; repeatable.",
    )
    parser.add_argument(
        "--all-in-dependency-work-unit-id",
        "--all-in-dependency-lifecycle-id",
        dest="all_in_dependency_work_unit_id",
        action="append",
        default=[],
        help="Directly required outside-pipeline work unit; repeatable.",
    )
    parser.add_argument(
        "--system-fingerprint",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Repeatable system fingerprint component.",
    )


def _add_action_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--actor",
        choices=("unknown", "human", "supervising_agent"),
        default=None,
        help="Verified actor; omitted direct launches remain unknown.",
    )
    parser.add_argument(
        "--action-family",
        choices=tuple(sorted(ACTION_FAMILIES)),
        default="launch",
    )
    parser.add_argument("--operation", default="cli_execution")
    parser.add_argument("--interface", default="cli")
    parser.add_argument(
        "--work-scope",
        choices=(
            "qualification",
            "implementation",
            "supervising_agent",
            "outside_platform",
            "measurement",
        ),
        default="outside_platform",
    )
    parser.add_argument("--passive-observation", action="store_true")
    parser.add_argument("--policy-mandated", action="store_true")
    parser.add_argument("--measurement-administration", action="store_true")
    parser.add_argument(
        "--not-required-for-progress",
        dest="required_for_progress",
        action="store_false",
        default=True,
    )


def add_telemetry_command(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    telemetry = sub.add_parser(
        "telemetry",
        help="Record and materialize observational pipeline telemetry.",
    )
    telemetry_sub = telemetry.add_subparsers(dest="telemetry_cmd", required=True)

    execute = telemetry_sub.add_parser(
        "exec",
        help="Run an arbitrary command inside a measured lifecycle boundary.",
    )
    _add_context_arguments(execute)
    _add_action_arguments(execute)
    execute.add_argument("--cwd", type=Path)
    execute.add_argument(
        "--active-seconds",
        type=float,
        help=(
            "Explicit active time inside the measured subprocess interval. "
            "Without this value, direct human, supervisor, and unknown boundaries "
            "retain active time as unknown."
        ),
    )
    execute.add_argument(
        "--attempt-group-id",
        help=(
            "Stable retry-group identity. Successful attempts resolve prior measured "
            "failures in the same group; when omitted, a group is derived from the "
            "verified lifecycle context and command boundary."
        ),
    )
    execute.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command argv, normally supplied after --.",
    )
    execute.set_defaults(func=_cmd_telemetry_exec)

    action = telemetry_sub.add_parser(
        "action",
        help="Record unavoidable manual UI or external-system actions.",
    )
    action_sub = action.add_subparsers(dest="telemetry_action_cmd", required=True)
    record = action_sub.add_parser("record", help="Record one completed manual action.")
    _add_context_arguments(record)
    _add_action_arguments(record)
    record.add_argument("--started-at", required=True)
    record.add_argument("--ended-at")
    record.add_argument("--active-seconds", type=float)
    record.add_argument("--machine-wait-seconds", type=float, default=0.0)
    record.add_argument("--external-wait-seconds", type=float, default=0.0)
    record.add_argument(
        "--wait-category",
        choices=("queue", "provider", "ci", "approval", "external", "unknown"),
    )
    record.add_argument("--result", required=True)
    record.add_argument("--evidence-path", action="append", default=[])
    record.add_argument("--error-cluster-id", action="append", default=[])
    record.add_argument("--intervention-id")
    record.set_defaults(func=_cmd_telemetry_action_record)

    validate = telemetry_sub.add_parser(
        "validate",
        help="Validate a lifecycle event stream and report its retained event count.",
    )
    validate.add_argument("--events", required=True, type=Path)
    validate.set_defaults(func=_cmd_telemetry_validate)

    materialize = telemetry_sub.add_parser(
        "materialize",
        help="Derive case/cohort metrics from retained lifecycle telemetry.",
    )
    materialize.add_argument(
        "--events",
        action="append",
        default=[],
        type=Path,
        help="Authoritative lifecycle JSONL path; repeat for multiple streams.",
    )
    materialize.add_argument(
        "--discover-root",
        action="append",
        default=[],
        type=Path,
        help="Discover lifecycle_events.jsonl recursively; repeatable.",
    )
    materialize.add_argument("--output-dir", required=True, type=Path)
    materialize.add_argument("--cohort-id")
    materialize.add_argument(
        "--case-lifecycle-id",
        action="append",
        default=[],
        help="Restrict cohort aggregation while retaining the complete case report.",
    )
    materialize.add_argument(
        "--compare-to",
        type=Path,
        help="Prior cohort_metrics.json used for factual before/after deltas.",
    )
    materialize.set_defaults(func=_cmd_telemetry_materialize)


def _fingerprint_values(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name.strip() or not value.strip():
            raise ValueError("--system-fingerprint values must use NAME=VALUE")
        result[name.strip()] = value.strip()
    return result


def _context_from_args(args: argparse.Namespace) -> tuple[LifecycleContext, bool]:
    parent = load_context_from_env(required=False)
    supplied_fingerprint = _fingerprint_values(list(args.system_fingerprint))
    parent_fingerprint = parent.system_fingerprint if parent is not None else {}
    verified_parent = bool(
        parent is not None
        and parent.system_fingerprint.get("controller_context_verified") == "true"
    )
    fingerprint = {**parent_fingerprint, **supplied_fingerprint}
    if verified_parent:
        fingerprint["controller_context_verified"] = "true"
    else:
        fingerprint.pop("controller_context_verified", None)

    lifecycle_id = args.case_lifecycle_id or (
        parent.case_lifecycle_id if parent is not None else None
    )
    cycle_id = args.cycle_id or (parent.cycle_id if parent is not None else None)
    if lifecycle_id is None and cycle_id is None:
        cycle_id = f"external:{uuid.uuid4()}"
    context = LifecycleContext(
        case_lifecycle_id=lifecycle_id,
        case_id=args.case_id or (parent.case_id if parent is not None else None),
        cycle_id=cycle_id,
        stage=args.stage or (parent.stage if parent is not None else None),
        milestone_id=args.milestone_id
        or (parent.milestone_id if parent is not None else None),
        work_unit_id=args.work_unit_id
        or (parent.work_unit_id if parent is not None else None),
        session_id=parent.session_id if parent is not None else None,
        shared_work_id=args.shared_work_id
        or (parent.shared_work_id if parent is not None else None),
        parent_action_id=parent.parent_action_id if parent is not None else None,
        system_fingerprint=fingerprint,
    )
    return context, verified_parent


def _event_actor(args: argparse.Namespace, *, verified_parent: bool) -> tuple[str, str, str]:
    if args.actor in {"human", "supervising_agent"}:
        return args.actor, args.actor, (
            "manual" if args.actor == "human" else "supervising_agent"
        )
    if verified_parent:
        return "controller", "controller", "automatic"
    return "unknown", "unknown", "unknown_external"


def _action_attributes(args: argparse.Namespace, **extra: Any) -> dict[str, Any]:
    return {
        "action_family": args.action_family,
        "operation": args.operation,
        "interface": args.interface,
        "required_for_progress": bool(args.required_for_progress),
        "passive_observation": bool(args.passive_observation),
        "policy_mandated": bool(args.policy_mandated),
        "measurement_administration": bool(args.measurement_administration),
        "work_scope": args.work_scope,
        "dependency_ids": list(args.dependency_work_unit_id),
        "all_in_dependency_ids": list(args.all_in_dependency_work_unit_id),
        **extra,
    }


def _clean_command(command: list[str]) -> list[str]:
    cleaned = list(command)
    if cleaned and cleaned[0] == "--":
        cleaned = cleaned[1:]
    if not cleaned:
        raise ValueError("telemetry exec requires a command after --")
    return cleaned


def _concrete_action_work_identity(
    args: argparse.Namespace, context: LifecycleContext
) -> tuple[str, str | None, list[str]]:
    inherited_work_unit_id = context.work_unit_id if args.work_unit_id is None else None
    concrete_work_unit_id = args.work_unit_id or f"work:{uuid.uuid4()}"
    dependencies = list(args.dependency_work_unit_id)
    if (
        inherited_work_unit_id is not None
        and inherited_work_unit_id != concrete_work_unit_id
        and inherited_work_unit_id not in dependencies
    ):
        dependencies.append(inherited_work_unit_id)
    return concrete_work_unit_id, inherited_work_unit_id, dependencies


def _telemetry_exec_attempt_group_id(
    args: argparse.Namespace,
    context: LifecycleContext,
    *,
    command_fingerprint: str,
) -> str:
    explicit = str(args.attempt_group_id or "").strip()
    if args.attempt_group_id is not None and not explicit:
        raise ValueError("--attempt-group-id must not be empty")
    if explicit:
        return explicit
    scope_id = (
        context.case_lifecycle_id
        or context.cycle_id
        or context.work_unit_id
        or f"unscoped:{uuid.uuid4()}"
    )
    cwd = str(args.cwd.resolve()) if args.cwd is not None else str(Path.cwd().resolve())
    correlation_key = "\n".join(
        (
            scope_id,
            context.stage or "",
            context.milestone_id or "",
            context.work_unit_id or "",
            str(args.operation),
            cwd,
            command_fingerprint,
        )
    )
    return f"exec-attempt-group:{uuid.uuid5(uuid.NAMESPACE_URL, correlation_key)}"


def _open_telemetry_exec_errors(
    events_path: Path,
    *,
    attempt_group_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    open_clusters: dict[str, dict[str, Any]] = {}
    for event in read_lifecycle_events(events_path):
        cluster_id = event.error_cluster_id
        if cluster_id is None:
            continue
        if event.attributes.get("telemetry_exec_attempt_group_id") != attempt_group_id:
            continue
        if event.event_type == "error.occurred":
            open_clusters[cluster_id] = dict(event.attributes)
        elif event.event_type == "error.resolved":
            open_clusters.pop(cluster_id, None)
    return list(open_clusters.items())


def _telemetry_exec_resolution_mode(actor: str) -> str:
    if actor == "controller":
        return "self_healed_controller"
    if actor == "supervising_agent":
        return "resolved_supervisor"
    if actor == "human":
        return "resolved_human"
    return "resolved_external"


def _cmd_telemetry_exec(args: argparse.Namespace) -> int:
    context, verified_parent = _context_from_args(args)
    actor, initiator, origin = _event_actor(args, verified_parent=verified_parent)
    command = _clean_command(list(args.command))
    explicit_active_seconds = (
        float(args.active_seconds) if args.active_seconds is not None else None
    )
    if explicit_active_seconds is not None and explicit_active_seconds < 0:
        raise ValueError("--active-seconds must be non-negative")
    redacted = redact_command(command)
    command_fingerprint = fingerprint_command(redacted)
    attempt_group_id = _telemetry_exec_attempt_group_id(
        args,
        context,
        command_fingerprint=command_fingerprint,
    )
    action_id = f"action:{uuid.uuid4()}"
    invocation_id = f"invocation:{uuid.uuid4()}"
    concrete_work_unit_id, inherited_work_unit_id, dependencies = (
        _concrete_action_work_identity(args, context)
    )
    child_context = replace(
        context,
        work_unit_id=concrete_work_unit_id,
        invocation_id=invocation_id,
        parent_action_id=action_id,
        system_fingerprint={
            **context.system_fingerprint,
            "launch_origin": origin,
            **(
                {"controller_context_verified": "true"}
                if verified_parent
                else {}
            ),
        },
    )
    started_at = utc_now()
    common = {
        "actor_type": actor,
        "initiator_type": initiator,
        "root_initiator_type": initiator,
        "origin": origin,
        "provenance_quality": "authoritative" if verified_parent or args.actor else "unknown",
    }
    child_is_automatic = verified_parent and args.actor is None
    if not child_is_automatic:
        child_context = replace(
            child_context,
            system_fingerprint={
                key: value
                for key, value in child_context.system_fingerprint.items()
                if key != "controller_context_verified"
            },
        )
    append_lifecycle_event(
        args.events,
        make_lifecycle_event(
            "action.started",
            child_context,
            idempotency_key=f"{action_id}:started",
            occurred_at=started_at,
            started_at=started_at,
            attributes=_action_attributes(
                args,
                action_id=action_id,
                telemetry_exec_timing_version=2,
                parent_work_unit_id=inherited_work_unit_id,
                dependency_ids=dependencies,
                command_family=command_family(command),
                redacted_command=redacted,
                command_fingerprint=command_fingerprint,
                telemetry_exec_attempt_group_id=attempt_group_id,
            ),
            beneficiary_case_lifecycle_ids=tuple(
                args.beneficiary_case_lifecycle_id
            ),
            **common,
        ),
    )

    started_monotonic = time.monotonic()
    result_code: int
    execution_error: BaseException | None = None
    try:
        child_env = dict(os.environ)
        child_env.update(lifecycle_context_env(child_context))
        completed = subprocess.run(
            command,
            cwd=args.cwd,
            env=child_env,
            check=False,
        )
        result_code = int(completed.returncode)
    except BaseException as exc:  # preserve an auditable terminal event before re-raising
        execution_error = exc
        result_code = 1
    ended_at = utc_now()
    subprocess_wall_seconds = max(0.0, time.monotonic() - started_monotonic)
    active_seconds = (
        explicit_active_seconds
        if explicit_active_seconds is not None
        else subprocess_wall_seconds
        if child_is_automatic
        else None
    )
    resource_time_complete = (
        active_seconds is not None
        and active_seconds + 1e-6 >= subprocess_wall_seconds
    )
    resource_time_unknown_reason: str | None = None
    if not resource_time_complete:
        if explicit_active_seconds is not None:
            resource_time_unknown_reason = "subprocess_interval_partially_classified"
        elif args.actor in {"human", "supervising_agent"}:
            resource_time_unknown_reason = "manual_boundary_child_time_unattributable"
        else:
            resource_time_unknown_reason = "external_boundary_time_unattributable"
    completed_event = make_lifecycle_event(
        "action.completed",
        child_context,
        idempotency_key=f"{action_id}:completed",
        occurred_at=ended_at,
        started_at=started_at,
        ended_at=ended_at,
        active_seconds=active_seconds,
        attributes=_action_attributes(
            args,
            action_id=action_id,
            telemetry_exec_timing_version=2,
            parent_work_unit_id=inherited_work_unit_id,
            dependency_ids=dependencies,
            command_family=command_family(command),
            redacted_command=redacted,
            command_fingerprint=command_fingerprint,
            telemetry_exec_attempt_group_id=attempt_group_id,
            subprocess_wall_seconds=subprocess_wall_seconds,
            active_seconds_source=(
                "explicit"
                if explicit_active_seconds is not None
                else "verified_automatic_subprocess_wall"
                if child_is_automatic
                else "unknown"
            ),
            resource_time_unknown=not resource_time_complete,
            resource_time_unknown_reason=resource_time_unknown_reason,
            exit_code=result_code,
            result="success" if result_code == 0 else "failure",
            execution_error_type=(
                type(execution_error).__name__ if execution_error is not None else None
            ),
        ),
        beneficiary_case_lifecycle_ids=tuple(args.beneficiary_case_lifecycle_id),
        **common,
    )
    append_lifecycle_event(
        args.events,
        completed_event,
    )
    open_errors = _open_telemetry_exec_errors(
        args.events,
        attempt_group_id=attempt_group_id,
    )
    if result_code != 0:
        error_kind = (
            f"subprocess_exception:{type(execution_error).__name__}"
            if execution_error is not None
            else "process_exit_nonzero"
        )
        matching_clusters = [
            cluster_id
            for cluster_id, attributes in open_errors
            if attributes.get("error_kind") == error_kind
            and attributes.get("command_fingerprint") == command_fingerprint
        ]
        cluster_id = (
            matching_clusters[-1] if matching_clusters else f"error:telemetry-exec:{uuid.uuid4()}"
        )
        append_lifecycle_event(
            args.events,
            make_lifecycle_event(
                "error.occurred",
                child_context,
                idempotency_key=f"{cluster_id}:occurrence:{action_id}",
                occurred_at=ended_at,
                parent_event_id=completed_event.event_id,
                error_cluster_id=cluster_id,
                beneficiary_case_lifecycle_ids=tuple(args.beneficiary_case_lifecycle_id),
                attributes={
                    "error_kind": error_kind,
                    "exit_code": result_code,
                    "action_id": action_id,
                    "command_family": command_family(command),
                    "command_fingerprint": command_fingerprint,
                    "telemetry_exec_attempt_group_id": attempt_group_id,
                    "terminal": False,
                },
                **common,
            ),
        )
    elif open_errors:
        resolution_mode = _telemetry_exec_resolution_mode(actor)
        for cluster_id, attributes in open_errors:
            append_lifecycle_event(
                args.events,
                make_lifecycle_event(
                    "error.resolved",
                    child_context,
                    idempotency_key=f"{cluster_id}:resolved",
                    occurred_at=ended_at,
                    parent_event_id=completed_event.event_id,
                    error_cluster_id=cluster_id,
                    beneficiary_case_lifecycle_ids=tuple(args.beneficiary_case_lifecycle_id),
                    attributes={
                        "error_kind": attributes.get("error_kind"),
                        "resolution_mode": resolution_mode,
                        "resolution_action_id": action_id,
                        "resolution_work_unit_ids": [concrete_work_unit_id],
                        "resolution_cost_attribution_complete": True,
                        "telemetry_exec_attempt_group_id": attempt_group_id,
                    },
                    **common,
                ),
            )
    _refresh_metrics_after_write(args.events)
    if execution_error is not None:
        raise execution_error
    return result_code


def _parse_timestamp(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _cmd_telemetry_action_record(args: argparse.Namespace) -> int:
    context, verified_parent = _context_from_args(args)
    actor, initiator, origin = _event_actor(args, verified_parent=verified_parent)
    if actor not in {"human", "supervising_agent"}:
        raise ValueError("action record requires --actor human or --actor supervising_agent")
    start = _parse_timestamp(args.started_at, label="--started-at")
    machine_wait_seconds = float(args.machine_wait_seconds)
    external_wait_seconds = float(args.external_wait_seconds)
    if machine_wait_seconds < 0 or external_wait_seconds < 0:
        raise ValueError("wait seconds must be non-negative")
    if (
        machine_wait_seconds + external_wait_seconds > 0
        and args.wait_category is None
    ):
        raise ValueError("--wait-category is required when wait time is recorded")
    if args.ended_at is not None:
        end = _parse_timestamp(args.ended_at, label="--ended-at")
    elif args.active_seconds is not None or (
        machine_wait_seconds + external_wait_seconds > 0
    ):
        end = start + timedelta(
            seconds=(
                (args.active_seconds or 0.0)
                + machine_wait_seconds
                + external_wait_seconds
            )
        )
    else:
        end = start
    if end < start:
        raise ValueError("--ended-at must not precede --started-at")
    active_seconds = (
        args.active_seconds
        if args.active_seconds is not None
        else max(
            0.0,
            (end - start).total_seconds()
            - machine_wait_seconds
            - external_wait_seconds,
        )
    )
    if active_seconds < 0:
        raise ValueError("--active-seconds must be non-negative")
    wall_seconds = max(0.0, (end - start).total_seconds())
    classified_seconds = active_seconds + machine_wait_seconds + external_wait_seconds
    if classified_seconds > wall_seconds + 1e-6:
        raise ValueError("active and wait seconds must not exceed the action interval")
    action_id = f"action:{uuid.uuid4()}"
    concrete_work_unit_id, inherited_work_unit_id, dependencies = (
        _concrete_action_work_identity(args, context)
    )
    action_context = replace(
        context,
        work_unit_id=concrete_work_unit_id,
        parent_action_id=action_id,
    )
    append_lifecycle_event(
        args.events,
        make_lifecycle_event(
            "action.completed",
            action_context,
            idempotency_key=f"{action_id}:recorded",
            occurred_at=_format_timestamp(end),
            started_at=_format_timestamp(start),
            ended_at=_format_timestamp(end),
            active_seconds=active_seconds,
            machine_wait_seconds=machine_wait_seconds,
            external_wait_seconds=external_wait_seconds,
            actor_type=actor,
            initiator_type=initiator,
            root_initiator_type=initiator,
            origin=origin,
            intervention_id=args.intervention_id,
            error_cluster_id=(
                args.error_cluster_id[0] if len(args.error_cluster_id) == 1 else None
            ),
            evidence_paths=tuple(args.evidence_path),
            beneficiary_case_lifecycle_ids=tuple(
                args.beneficiary_case_lifecycle_id
            ),
            provenance_quality="operator_attested",
            attributes=_action_attributes(
                args,
                action_id=action_id,
                parent_work_unit_id=inherited_work_unit_id,
                dependency_ids=dependencies,
                result=args.result,
                related_error_cluster_ids=list(args.error_cluster_id),
                wait_category=args.wait_category,
                wait_seconds_by_category=(
                    {
                        args.wait_category: (
                            machine_wait_seconds + external_wait_seconds
                        )
                    }
                    if args.wait_category is not None
                    else {}
                ),
            ),
        ),
    )
    _refresh_metrics_after_write(args.events)
    print(action_id)
    return 0


def _cmd_telemetry_validate(args: argparse.Namespace) -> int:
    events = read_lifecycle_events(args.events)
    print(f"valid lifecycle events: {len(events)}")
    return 0


def _refresh_metrics_after_write(events_path: Path) -> None:
    """Refresh derived artifacts without making telemetry an operational gate."""

    try:
        materialize_lifecycle_metrics(
            event_sources=[events_path],
            output_dir=events_path.parent,
        )
    except Exception as exc:  # noqa: BLE001 - disposition must survive metric outages
        warnings.warn(
            f"automatic metrics refresh failed for {events_path}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )


def _cmd_telemetry_materialize(args: argparse.Namespace) -> int:
    sources = {path.resolve() for path in args.events}
    sources.update(discover_lifecycle_event_logs(args.discover_root))
    if not sources:
        raise ValueError("telemetry materialize requires --events or --discover-root")
    result = materialize_lifecycle_metrics(
        event_sources=sorted(sources, key=lambda path: path.as_posix().casefold()),
        output_dir=args.output_dir,
        cohort_id=args.cohort_id,
        case_lifecycle_ids=args.case_lifecycle_id or None,
        comparison_cohort=args.compare_to,
    )
    print(
        json.dumps(
            {
                "case_metrics": str(result.case_metrics_path),
                "cohort_metrics": str(result.cohort_metrics_path),
                "comparison": (
                    str(result.comparison_path) if result.comparison_path is not None else None
                ),
                "source_event_count": result.source_event_count,
                "retained_event_count": result.retained_event_count,
            },
            sort_keys=True,
        ),
        file=sys.stdout,
    )
    return 0


__all__ = [
    "add_telemetry_command",
    "_cmd_telemetry_action_record",
    "_cmd_telemetry_exec",
    "_cmd_telemetry_materialize",
    "_cmd_telemetry_validate",
]
