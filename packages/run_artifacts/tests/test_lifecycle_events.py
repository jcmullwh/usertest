from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from run_artifacts.lifecycle_events import (
    LIFECYCLE_CONTEXT_ENV,
    LIFECYCLE_CONTEXT_FILE_ENV,
    ErrorCluster,
    IdempotencyConflictError,
    Intervention,
    LifecycleContext,
    LifecycleEvent,
    LifecycleManifest,
    ManualAction,
    ModelUsageReceipt,
    TelemetryArtifactError,
    TelemetryValidationError,
    append_lifecycle_event,
    command_family,
    deserialize_lifecycle_context,
    fingerprint_command,
    lifecycle_context_env,
    load_context_from_env,
    make_lifecycle_event,
    read_lifecycle_context,
    read_lifecycle_events,
    read_lifecycle_manifest,
    read_model_usage_receipt,
    redact_command,
    serialize_lifecycle_context,
    validate_error_cluster,
    validate_intervention,
    validate_lifecycle_context,
    validate_lifecycle_event,
    validate_manual_action,
    validate_model_usage_receipt,
    write_content_addressed_model_usage_receipt,
    write_lifecycle_context,
    write_lifecycle_manifest,
    write_model_usage_receipt,
)

T0 = "2026-07-09T12:00:00Z"
T1 = "2026-07-09T12:01:00Z"
T2 = "2026-07-09T12:02:00Z"
SHA_A = "a" * 64


def _context(**changes: object) -> LifecycleContext:
    values: dict[str, object] = {
        "case_lifecycle_id": "lifecycle-1",
        "case_id": "case-1",
        "cycle_id": "cycle-1",
        "stage": "qualification",
        "milestone_id": "stage-1",
        "work_unit_id": "work-1",
        "invocation_id": "invocation-1",
        "session_id": "session-1",
        "shared_work_id": None,
        "parent_action_id": "action-parent",
        "system_fingerprint": {
            "code_commit": "abc123",
            "model": "gpt-5.6",
            "score_version": "1",
        },
    }
    values.update(changes)
    return LifecycleContext(**values)  # type: ignore[arg-type]


def _event(
    event_id: str = "event-1",
    idempotency_key: str = "case-1:stage-1:started",
    **changes: object,
) -> LifecycleEvent:
    values: dict[str, object] = {
        "event_id": event_id,
        "event_type": "stage.started",
        "occurred_at": T0,
        "recorded_at": T0,
        "context": _context(),
        "idempotency_key": idempotency_key,
        "actor_type": "controller",
        "initiator_type": "controller",
        "root_initiator_type": "controller",
        "origin": "automatic",
        "started_at": T0,
        "beneficiary_case_lifecycle_ids": ("lifecycle-1",),
        "evidence_paths": ("runs/stage-1/report.json",),
        "artifact_hashes": {"report": SHA_A},
        "provenance_quality": "authoritative",
        "attributes": {"attempt": 1, "resumed": False},
    }
    values.update(changes)
    return LifecycleEvent(**values)  # type: ignore[arg-type]


def test_context_round_trip_through_json_environment_and_file(tmp_path: Path) -> None:
    context = _context()

    serialized = serialize_lifecycle_context(context)
    assert deserialize_lifecycle_context(serialized) == context
    assert lifecycle_context_env(context) == {LIFECYCLE_CONTEXT_ENV: serialized}
    assert load_context_from_env({LIFECYCLE_CONTEXT_ENV: serialized}) == context

    context_path = tmp_path / "context.json"
    digest = write_lifecycle_context(context_path, context)
    assert len(digest) == 64
    assert read_lifecycle_context(context_path) == context
    assert load_context_from_env({LIFECYCLE_CONTEXT_FILE_ENV: str(context_path)}) == context
    assert (
        load_context_from_env(
            {
                LIFECYCLE_CONTEXT_ENV: serialized,
                LIFECYCLE_CONTEXT_FILE_ENV: str(context_path),
            }
        )
        == context
    )


def test_context_environment_rejects_absence_disagreement_and_invalid_context(
    tmp_path: Path,
) -> None:
    assert load_context_from_env({}) is None
    with pytest.raises(TelemetryValidationError, match="missing USERTEST_LIFECYCLE_CONTEXT"):
        load_context_from_env({}, required=True)

    other_path = tmp_path / "other.json"
    write_lifecycle_context(other_path, _context(case_lifecycle_id="lifecycle-2"))
    with pytest.raises(TelemetryValidationError, match="disagree"):
        load_context_from_env(
            {
                LIFECYCLE_CONTEXT_ENV: serialize_lifecycle_context(_context()),
                LIFECYCLE_CONTEXT_FILE_ENV: str(other_path),
            }
        )

    with pytest.raises(TelemetryValidationError, match="requires"):
        validate_lifecycle_context(LifecycleContext())
    with pytest.raises(TelemetryValidationError, match="unknown fields"):
        deserialize_lifecycle_context('{"schema_version":1,"surprise":true}')


def test_event_round_trip_and_make_helper() -> None:
    event = _event()
    assert LifecycleEvent.from_dict(event.to_dict()) == event
    assert validate_lifecycle_event(event) is event

    made = make_lifecycle_event(
        "lifecycle.admitted",
        _context(),
        idempotency_key="admission:case-1",
        occurred_at=T0,
        actor_type="controller",
        initiator_type="controller",
        root_initiator_type="controller",
        origin="automatic",
    )
    assert made.event_type == "lifecycle.admitted"
    assert made.idempotency_key == "admission:case-1"
    assert made.event_id


def test_event_validation_rejects_invalid_time_hash_enum_and_schema() -> None:
    with pytest.raises(TelemetryValidationError, match="must not precede"):
        validate_lifecycle_event(_event(started_at=T1, ended_at=T0))
    with pytest.raises(TelemetryValidationError, match="SHA-256"):
        validate_lifecycle_event(_event(artifact_hashes={"report": "not-a-hash"}))
    with pytest.raises(TelemetryValidationError, match="actor_type must be one of"):
        validate_lifecycle_event(_event(actor_type="robot"))
    with pytest.raises(TelemetryValidationError, match="schema_version must be 1"):
        validate_lifecycle_event(replace(_event(), schema_version=2))


def test_jsonl_append_is_idempotent_and_detects_event_id_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle_events.jsonl"
    first = _event()

    assert append_lifecycle_event(path, first) is True
    assert append_lifecycle_event(path, first) is False
    duplicate_operation = _event(
        event_id="event-regenerated",
        idempotency_key=first.idempotency_key,
        occurred_at=T1,
    )
    assert append_lifecycle_event(path, duplicate_operation) is False
    assert read_lifecycle_events(path) == [first]

    conflicting_id = replace(first, event_type="stage.completed")
    with pytest.raises(IdempotencyConflictError, match="different content"):
        append_lifecycle_event(path, conflicting_id)


def test_jsonl_append_rejects_rebinding_work_unit_to_another_action(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lifecycle_events.jsonl"
    started = _event(
        event_id="action-a-started",
        idempotency_key="action-a:started",
        event_type="action.started",
        attributes={"action_id": "action-a"},
    )
    completed = _event(
        event_id="action-a-completed",
        idempotency_key="action-a:completed",
        event_type="action.completed",
        occurred_at=T1,
        ended_at=T1,
        attributes={"action_id": "action-a"},
    )
    rebound = _event(
        event_id="action-b-started",
        idempotency_key="action-b:started",
        event_type="action.started",
        occurred_at=T2,
        started_at=T2,
        attributes={"action_id": "action-b"},
    )
    action_rebound = _event(
        event_id="action-a-rebound",
        idempotency_key="action-a:rebound",
        event_type="action.completed",
        occurred_at=T2,
        ended_at=T2,
        context=_context(work_unit_id="work-2"),
        attributes={"action_id": "action-a"},
    )

    assert append_lifecycle_event(path, started)
    assert append_lifecycle_event(path, completed)
    with pytest.raises(IdempotencyConflictError, match="already bound to action"):
        append_lifecycle_event(path, rebound)
    with pytest.raises(IdempotencyConflictError, match="already bound to work unit"):
        append_lifecycle_event(path, action_rebound)
    assert read_lifecycle_events(path) == [started, completed]


def test_jsonl_append_repairs_only_a_truncated_tail(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle_events.jsonl"
    first = _event()
    assert append_lifecycle_event(path, first)
    with path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"event_id":"partial')

    second = _event("event-2", "case-1:stage-1:completed", occurred_at=T1)
    assert append_lifecycle_event(path, second)
    assert read_lifecycle_events(path) == [first, second]

    path.write_bytes(b'{"broken":}\n')
    with pytest.raises(TelemetryArtifactError, match="invalid lifecycle event JSON"):
        append_lifecycle_event(path, first)


def test_jsonl_append_handles_a_valid_final_line_without_newline(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle_events.jsonl"
    first = _event()
    path.write_text(json.dumps(first.to_dict()), encoding="utf-8")

    second = _event("event-2", "case-1:stage-1:completed", occurred_at=T1)
    assert append_lifecycle_event(path, second)
    assert read_lifecycle_events(path) == [first, second]
    assert path.read_bytes().endswith(b"\n")


def test_jsonl_append_serializes_concurrent_writers(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle_events.jsonl"
    events = [
        _event(f"event-{index}", f"operation-{index}", occurred_at=T0)
        for index in range(20)
    ]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda event: append_lifecycle_event(path, event), events))

    assert results == [True] * 20
    loaded = read_lifecycle_events(path)
    assert {event.event_id for event in loaded} == {event.event_id for event in events}


@pytest.mark.skipif(os.name != "nt", reason="Windows lock contention semantics")
def test_jsonl_append_retries_windows_lock_permission_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lifecycle_events.jsonl"
    original_open = os.open
    permission_race_observed = False

    def _open_with_one_permission_race(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal permission_race_observed
        if not permission_race_observed and str(target).endswith(".lock"):
            permission_race_observed = True
            raise PermissionError(13, "simulated Windows sharing violation", str(target))
        return original_open(target, flags, mode)

    monkeypatch.setattr(os, "open", _open_with_one_permission_race)

    assert append_lifecycle_event(path, _event()) is True
    assert permission_race_observed is True
    assert len(read_lifecycle_events(path)) == 1


def test_jsonl_reader_rejects_duplicate_ids_and_idempotency_keys(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle_events.jsonl"
    first = _event()
    second = _event("event-2", first.idempotency_key)
    path.write_text(
        "\n".join(json.dumps(event.to_dict()) for event in (first, second)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TelemetryArtifactError, match="duplicate idempotency_key"):
        read_lifecycle_events(path)


def test_per_invocation_usage_receipt_round_trip_and_content_addressing(
    tmp_path: Path,
) -> None:
    receipt = ModelUsageReceipt(
        receipt_id="usage-1",
        context=_context(),
        provider="openai",
        model="gpt-5.6",
        usage_semantics="per_invocation",
        recorded_at=T1,
        invocation_started_at=T0,
        invocation_ended_at=T1,
        input_tokens=100,
        cached_input_tokens=40,
        uncached_input_tokens=60,
        output_tokens=20,
        reasoning_tokens=5,
        total_tokens=120,
        observed_usage={
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "uncached_input_tokens": 60,
            "output_tokens": 20,
            "reasoning_tokens": 5,
            "total_tokens": 120,
        },
        source_artifact_path="sessions/session-1.jsonl",
        source_artifact_sha256=SHA_A,
    )
    assert ModelUsageReceipt.from_dict(receipt.to_dict()) == receipt
    assert validate_model_usage_receipt(receipt) is receipt
    with pytest.raises(TelemetryValidationError, match="per-invocation observed usage"):
        validate_model_usage_receipt(replace(receipt, total_tokens=121))

    path = write_content_addressed_model_usage_receipt(tmp_path / "receipts", receipt)
    assert path.name == "model_usage_receipt.json"
    assert len(path.parent.name) == 64
    assert read_model_usage_receipt(path) == receipt
    assert write_content_addressed_model_usage_receipt(tmp_path / "receipts", receipt) == path


def test_usage_receipt_cumulative_delta_and_unattributable_contracts() -> None:
    cumulative = ModelUsageReceipt(
        receipt_id="usage-cumulative",
        context=_context(),
        provider="openai",
        model="gpt-5.6",
        usage_semantics="session_cumulative",
        recorded_at=T1,
        input_tokens=50,
        output_tokens=20,
        total_tokens=70,
        baseline_usage={"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
        observed_usage={"input_tokens": 150, "output_tokens": 50, "total_tokens": 200},
    )
    assert validate_model_usage_receipt(cumulative) is cumulative

    with pytest.raises(TelemetryValidationError, match="observed-baseline delta"):
        validate_model_usage_receipt(replace(cumulative, input_tokens=150))
    with pytest.raises(TelemetryValidationError, match="below its baseline"):
        validate_model_usage_receipt(
            replace(
                cumulative,
                observed_usage={"input_tokens": 90, "output_tokens": 50, "total_tokens": 200},
            )
        )

    unattributable = ModelUsageReceipt(
        receipt_id="usage-unknown",
        context=_context(),
        provider="unknown-provider",
        model="unknown-model",
        usage_semantics="unattributable",
        recorded_at=T1,
        observed_usage={"total_tokens": 100},
        provenance_quality="unknown",
    )
    assert validate_model_usage_receipt(unattributable) is unattributable
    with pytest.raises(TelemetryValidationError, match="must not publish"):
        validate_model_usage_receipt(replace(unattributable, total_tokens=100))


def test_usage_receipt_exact_writer_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "invocation" / "model_usage_receipt.json"
    receipt = ModelUsageReceipt(
        receipt_id="usage-1",
        context=_context(),
        provider="openai",
        model="gpt-5.6",
        usage_semantics="per_invocation",
        recorded_at=T1,
        total_tokens=10,
    )
    digest = write_model_usage_receipt(path, receipt)
    assert digest == write_model_usage_receipt(path, receipt)
    with pytest.raises(IdempotencyConflictError, match="different content"):
        write_model_usage_receipt(path, replace(receipt, total_tokens=11))


def test_error_cluster_round_trip_and_resolution_invariants() -> None:
    cluster = ErrorCluster(
        error_cluster_id="error-1",
        context=_context(),
        error_kind="provider_timeout",
        first_occurred_at=T0,
        last_occurred_at=T1,
        occurrence_count=2,
        resolution_mode="self_healed_controller",
        resolved_at=T2,
        resolution_event_id="event-resolution",
        occurrence_event_ids=("event-error-1", "event-error-2"),
        token_usage_receipt_ids=("usage-1",),
    )
    assert ErrorCluster.from_dict(cluster.to_dict()) == cluster
    assert validate_error_cluster(cluster) is cluster

    with pytest.raises(TelemetryValidationError, match="open error clusters"):
        validate_error_cluster(replace(cluster, resolution_mode="open"))
    with pytest.raises(TelemetryValidationError, match="occurrence_count must match"):
        validate_error_cluster(replace(cluster, occurrence_count=3))


def test_intervention_and_manual_action_keep_units_separate() -> None:
    redacted = redact_command(["gh", "pr", "create", "--token", "super-secret"])
    action = ManualAction(
        action_id="action-1",
        context=_context(),
        action_family="pull_request",
        operation="create",
        interface="cli",
        actor_type="human",
        started_at=T0,
        ended_at=T1,
        active_seconds=60,
        policy_mandated=False,
        redacted_command=redacted,
        command_fingerprint=fingerprint_command(redacted),
        command_family="gh",
        related_error_cluster_ids=("error-1",),
        intervention_id="intervention-1",
    )
    assert ManualAction.from_dict(action.to_dict()) == action
    assert validate_manual_action(action) is action

    intervention = Intervention(
        intervention_id="intervention-1",
        context=_context(),
        intervention_kind="delivery_recovery",
        started_at=T0,
        ended_at=T1,
        active_seconds=60,
        actor_type="human",
        related_error_cluster_ids=("error-1",),
        action_ids=(action.action_id,),
        result="pr_created",
    )
    assert Intervention.from_dict(intervention.to_dict()) == intervention
    assert validate_intervention(intervention) is intervention

    with pytest.raises(TelemetryValidationError, match="human or supervising_agent"):
        validate_manual_action(replace(action, actor_type="controller"))
    with pytest.raises(TelemetryValidationError, match="does not match"):
        validate_manual_action(replace(action, command_fingerprint="b" * 64))
    with pytest.raises(TelemetryValidationError, match="human or supervising_agent"):
        validate_intervention(replace(intervention, actor_type="controller"))


def test_command_redaction_removes_common_secret_shapes_before_fingerprinting() -> None:
    command = (
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz "
        "curl https://person:hunter2@example.test/api "
        "--token ghp_abcdefghijklmnopqrstuvwxyz "
        "-H 'Authorization: Bearer abcdefghijklmnop'"
    )
    redacted = redact_command(command)

    assert "hunter2" not in redacted
    assert "ghp_" not in redacted
    assert "sk-" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert redacted.count("<redacted>") >= 4
    assert fingerprint_command(command) == fingerprint_command(redacted)

    argv = ["tool", "--api-key=secret-value", "--password", "another-secret", "run"]
    argv_redacted = redact_command(argv)
    assert "secret-value" not in argv_redacted
    assert "another-secret" not in argv_redacted
    assert argv_redacted.count("'") % 2 == 0
    quoted_argv = redact_command(["python", "-c", "x", "--api-key=secret"])
    assert quoted_argv == "python -c x '--api-key=<redacted>'"
    assert fingerprint_command(quoted_argv) == fingerprint_command(
        ["python", "-c", "x", "--api-key=secret"]
    )
    assert command_family(argv) == "tool"
    assert command_family("KEY=value gh pr create") == "gh"


def test_manifest_round_trip_atomic_write_and_path_validation(tmp_path: Path) -> None:
    manifest = LifecycleManifest(
        case_lifecycle_id="lifecycle-1",
        case_id="case-1",
        created_at=T0,
        updated_at=T1,
        status="active",
        dependency_lifecycle_ids=("shared-stage-lifecycle",),
        shared_work_ids=("shared-work-1",),
        usage_receipt_paths=(
            f"usage/{'a' * 64}/model_usage_receipt.json",
        ),
        system_fingerprint={"commit": "abc123", "model": "gpt-5.6"},
        metadata={"disposition": None},
    )
    path = tmp_path / "lifecycle_manifest.json"
    digest = write_lifecycle_manifest(path, manifest)
    assert len(digest) == 64
    assert read_lifecycle_manifest(path) == manifest

    updated = replace(manifest, updated_at=T2, status="terminal")
    assert write_lifecycle_manifest(path, updated) != digest
    assert read_lifecycle_manifest(path) == updated

    with pytest.raises(TelemetryValidationError, match="cannot depend on itself"):
        write_lifecycle_manifest(
            path, replace(manifest, dependency_lifecycle_ids=(manifest.case_lifecycle_id,))
        )
    with pytest.raises(TelemetryValidationError, match="non-escaping relative path"):
        write_lifecycle_manifest(path, replace(manifest, event_log_path="../other/events.jsonl"))


def test_public_models_reject_unknown_fields() -> None:
    raw = _event().to_dict()
    raw["unversioned_guess"] = True
    with pytest.raises(TelemetryValidationError, match="unknown fields"):
        LifecycleEvent.from_dict(raw)
