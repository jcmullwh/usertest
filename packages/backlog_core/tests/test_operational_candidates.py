from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from backlog_core.case_lineage import (
    assign_problem_case_ids,
    build_case_registry,
    eligible_problem_mining_atoms,
    load_case_registry,
    normalize_atom_lineage,
    write_case_registry,
)
from backlog_core.operational_candidates import (
    build_operational_failure_candidates,
    operational_candidate_receipt_errors,
)


def _record(
    run_id: str,
    *,
    status: str = "error",
    error: dict[str, object] | None = None,
    mission_id: str = "backlog_repro_research",
    report_status: str | None = None,
    metrics: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    record: dict[str, object] = {
        "run_rel": run_id,
        "status": status,
        "agent_exit_code": 1 if status == "error" else 0,
        "target_ref": {
            "mission_id": mission_id,
            "report_schema_path": "configs/report_schemas/troubleshoot_v1.schema.json",
        },
        "error": error,
        "metrics": metrics or {},
        "report_validation_errors": [],
        "terminal_artifact_reads": {},
    }
    if report_status is not None:
        record["report"] = {"kind": "troubleshoot_v1", "status": report_status}
    record.update(extra)
    return record


def _atom(
    run_id: str,
    atom_id: str,
    *,
    source: str = "run_failure_event",
    evidence_class: str = "observed",
    parent_case_id: str | None = "case:parent",
    role: str = "research",
    **extra: object,
) -> dict[str, object]:
    atom: dict[str, object] = {
        "atom_id": atom_id,
        "run_id": run_id,
        "run_rel": run_id,
        "origin_run_id": run_id,
        "source": source,
        "text": "Free-form derived text must not be mined directly.",
        "evidence_class": evidence_class,
        "evidence_role": role,
        "origin_stage": "repro_research" if role == "research" else role,
        "parent_case_id": parent_case_id,
        "case_id": parent_case_id,
        "supporting_case_ids": [parent_case_id] if parent_case_id else [],
        "disposition": "supports_case" if parent_case_id else "unresolved",
        "disposition_status": "decided" if parent_case_id else "pending",
        "lineage_authorities": ["runner_evidence_assignment"] if parent_case_id else [],
    }
    atom.update(extra)
    return atom


def _config_error() -> dict[str, object]:
    return {
        "type": "AgentConfigInvalid",
        "subtype": "invalid_agent_config",
        "code": "codex_model_messages_missing",
        "message": "This prose is intentionally excluded from the signature.",
    }


def test_identical_typed_failures_across_fourteen_parents_collapse() -> None:
    records: list[dict[str, object]] = []
    atoms: list[dict[str, object]] = []
    bindings: dict[str, dict[str, object]] = {}
    for index in range(14):
        run_id = f"research/run/{index}"
        case_id = f"case:{index:02d}"
        records.append(_record(run_id, error=_config_error()))
        atoms.append(
            _atom(
                run_id,
                f"{run_id}:run_failure_event:1",
                parent_case_id=case_id,
            )
        )
        bindings[run_id] = {
            "status": "verified",
            "case_ids": [case_id],
            "authority": "runner_evidence_assignment",
        }

    candidates = build_operational_failure_candidates(
        records,
        atoms,
        parent_bindings_by_run=bindings,
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    receipt = candidate["operational_candidate_receipt"]
    assert receipt["occurrence_count"] == 14
    assert receipt["parent_binding_statuses"] == ["verified"]
    assert receipt["related_parent_case_ids"] == [f"case:{index:02d}" for index in range(14)]
    assert candidate["operational_failure_class"] == "agent_config"
    assert candidate["evidence_role"] == "observation"
    assert candidate["disposition"] == "unresolved"
    assert operational_candidate_receipt_errors(candidate) == []


def test_explicit_repair_lineage_can_emit_operational_candidate_without_atoms() -> None:
    run_id = "research/repair/1"
    record = _record(
        run_id,
        error=_config_error(),
        target_ref={
            "mission_id": "ordinary_mission",
            "backlog_lineage": {
                "evidence_role": "research",
                "origin_stage": "repro_research_dossier_repair",
                "parent_case_id": "case:parent",
            },
        },
    )

    candidates = build_operational_failure_candidates(
        [record],
        [],
        parent_bindings_by_run={
            run_id: {
                "status": "verified",
                "case_ids": ["case:parent"],
                "authority": "runner_target_ref_lineage",
            }
        },
    )

    assert len(candidates) == 1
    receipt = candidates[0]["operational_candidate_receipt"]
    signal = receipt["typed_signal_receipts"][0]
    assert signal["origin_role"] == "research"
    assert signal["origin_stage"] == "repro_research_dossier_repair"


def test_unforeseen_runner_typed_blocker_kind_is_not_dropped() -> None:
    run_id = "research/run/unforeseen"
    record = _record(
        run_id,
        operational_failure_signals=[
            {
                "kind": "model_context_bridge",
                "phase": "context_materialization",
                "prevented_stage": True,
                "runner_attested": True,
                "producer": "runner_core.context_bridge",
                "error_type": "ContextBridgeUnavailable",
                "error_code": "context_bridge_unavailable",
            }
        ],
    )
    atom = _atom(run_id, f"{run_id}:run_failure_event:1")

    candidates = build_operational_failure_candidates([record], [atom])

    assert len(candidates) == 1
    assert candidates[0]["operational_failure_class"] == "model_context_bridge"
    assert candidates[0]["operational_failure_phase"] == "context_materialization"


def test_untyped_blocker_signal_and_generic_exec_prose_are_ignored() -> None:
    run_id = "research/run/unattested"
    record = _record(
        run_id,
        error={
            "type": "AgentExecFailed",
            "stderr": "A surprising context bridge failure prevented all work.",
        },
        operational_failure_signals=[
            {
                "kind": "model_context_bridge",
                "prevented_stage": True,
                "error_type": "ContextBridgeUnavailable",
            }
        ],
    )

    assert (
        build_operational_failure_candidates(
            [record],
            [_atom(run_id, f"{run_id}:run_failure_event:1")],
        )
        == []
    )


@pytest.mark.parametrize(
    "error",
    [
        {
            "type": "AgentExecFailed",
            "exit_code": 137,
            "stderr": "apply_patch verification failed: Failed to find expected lines",
        },
        {
            "type": "AgentExecFailed",
            "exit_code": 137,
            "stderr": (
                "[codex_warning_summary] code=codex_model_refresh_timeout "
                "classification=capability_notice"
            ),
        },
        {
            "type": "AgentExecFailed",
            "exit_code": 1,
            "stderr": "[synthetic_stderr] Request timed out",
            "stderr_synthesized": True,
        },
        {
            "type": "AgentExecFailed",
            "exit_code": 1,
            "stderr": "WARN codex_core_plugins::manifest: ignoring invalid plugin icon",
        },
    ],
    ids=("apply-patch", "model-refresh", "generic-timeout", "plugin-warning"),
)
def test_generic_agent_execution_failures_remain_parent_only(
    error: dict[str, object],
) -> None:
    run_id = "research/run/generic"

    assert (
        build_operational_failure_candidates(
            [_record(run_id, error=error)],
            [_atom(run_id, f"{run_id}:run_failure_event:1")],
        )
        == []
    )


def test_typed_disk_full_is_an_inspectable_infrastructure_candidate() -> None:
    run_id = "implementation/run/disk-full"
    candidate = build_operational_failure_candidates(
        [
            _record(
                run_id,
                mission_id="implement_maintenance_backlog_ticket_v1",
                error={
                    "type": "AgentExecFailed",
                    "subtype": "disk_full",
                    "exit_code": 1,
                    "stderr": "ENOSPC at C:/Users/private/runtime.log",
                    "last_message": "arbitrary agent prose",
                },
            )
        ],
        [_atom(run_id, f"{run_id}:run_failure_event:1", role="implementation")],
    )[0]

    signal_receipt = candidate["operational_candidate_receipt"]["typed_signal_receipts"][0]
    projection = signal_receipt["failure_evidence_projection"]
    assert candidate["operational_failure_class"] == "infrastructure"
    assert candidate["operational_failure_phase"] == "storage"
    assert projection["error"] == {
        "subtype": "disk_full",
        "type": "AgentExecFailed",
    }
    assert '"subtype":"disk_full"' in candidate["text"]
    assert "C:/Users/private" not in candidate["text"]
    assert "arbitrary agent prose" not in candidate["text"]
    assert operational_candidate_receipt_errors(candidate) == []


def test_typed_setup_runtime_error_disk_full_is_an_infrastructure_candidate() -> None:
    run_id = "implementation/run/setup-disk-full"
    candidate = build_operational_failure_candidates(
        [
            _record(
                run_id,
                mission_id="implement_maintenance_backlog_ticket_v1",
                error={
                    "type": "RuntimeError",
                    "subtype": "disk_full",
                    "message": "private free-form setup prose must not be classified",
                },
            )
        ],
        [_atom(run_id, f"{run_id}:run_failure_event:1", role="implementation")],
    )[0]

    projection = candidate["operational_candidate_receipt"]["typed_signal_receipts"][0][
        "failure_evidence_projection"
    ]
    assert candidate["operational_failure_class"] == "infrastructure"
    assert candidate["operational_failure_phase"] == "storage"
    assert projection["error"] == {
        "subtype": "disk_full",
        "type": "RuntimeError",
    }
    assert "private free-form" not in candidate["text"]
    assert operational_candidate_receipt_errors(candidate) == []


def test_generic_setup_runtime_error_prose_cannot_become_a_candidate() -> None:
    run_id = "implementation/run/generic-runtime-error"
    record = _record(
        run_id,
        mission_id="implement_maintenance_backlog_ticket_v1",
        error={
            "type": "RuntimeError",
            "message": (
                "No space left on device while cloning a transient workspace; "
                "the message even says disk_full but has no typed field."
            ),
        },
    )

    assert (
        build_operational_failure_candidates(
            [record],
            [_atom(run_id, f"{run_id}:run_failure_event:1", role="implementation")],
        )
        == []
    )


def test_disk_full_runner_envelopes_share_one_causal_candidate() -> None:
    runtime_run = "implementation/run/setup-runtime-disk-full"
    agent_exec_run = "implementation/run/agent-exec-disk-full"
    records = [
        _record(
            runtime_run,
            mission_id="implement_maintenance_backlog_ticket_v1",
            error={"type": "RuntimeError", "subtype": "disk_full", "phase": "setup"},
            target_ref={
                "mission_id": "implement_maintenance_backlog_ticket_v1",
                "execution_backend": "local",
            },
        ),
        _record(
            agent_exec_run,
            mission_id="implement_maintenance_backlog_ticket_v1",
            error={
                "type": "AgentExecFailed",
                "code": "disk_full",
                "failure_phase": "agent_execution",
            },
            target_ref={
                "mission_id": "implement_maintenance_backlog_ticket_v1",
                "execution_backend": "local",
                "report_schema_path": "configs/report_schemas/task_run_v1.schema.json",
            },
        ),
    ]
    atoms = [
        _atom(runtime_run, f"{runtime_run}:run_failure_event:1", role="implementation"),
        _atom(agent_exec_run, f"{agent_exec_run}:run_failure_event:1", role="implementation"),
    ]

    candidates = build_operational_failure_candidates(records, atoms)

    assert len(candidates) == 1
    candidate = candidates[0]
    receipt = candidate["operational_candidate_receipt"]
    assert receipt["occurrence_count"] == 2
    assert receipt["signature_fields"]["error_type"] == "storageexhausted"
    assert receipt["signature_fields"]["error_subtype"] == "disk_full"
    assert receipt["signature_fields"]["error_code"] is None
    assert receipt["signature_fields"]["phase"] == "storage"
    assert receipt["signature_fields"]["backend"] == "local"
    assert receipt["signature_fields"]["report_schema"] is None
    envelope_types = {
        signal["failure_evidence_projection"]["error"]["type"]
        for signal in receipt["typed_signal_receipts"]
    }
    assert envelope_types == {"AgentExecFailed", "RuntimeError"}
    raw_phases = {
        phase
        for signal in receipt["typed_signal_receipts"]
        for phase in (
            signal["failure_evidence_projection"]["error"].get("phase"),
            signal["failure_evidence_projection"]["error"].get("failure_phase"),
        )
        if phase is not None
    }
    assert raw_phases == {"agent_execution", "setup"}
    assert operational_candidate_receipt_errors(candidate) == []


def test_disk_full_known_backends_remain_distinct_storage_mechanisms() -> None:
    records: list[dict[str, object]] = []
    atoms: list[dict[str, object]] = []
    for backend in ("docker", "local"):
        run_id = f"implementation/run/{backend}-disk-full"
        records.append(
            _record(
                run_id,
                mission_id="implement_maintenance_backlog_ticket_v1",
                error={"type": "AgentExecFailed", "subtype": "disk_full"},
                target_ref={
                    "mission_id": "implement_maintenance_backlog_ticket_v1",
                    "execution_backend": backend,
                },
            )
        )
        atoms.append(_atom(run_id, f"{run_id}:run_failure_event:1", role="implementation"))

    candidates = build_operational_failure_candidates(records, atoms)

    assert len(candidates) == 2
    assert {
        candidate["operational_candidate_receipt"]["signature_fields"]["backend"]
        for candidate in candidates
    } == {"docker", "local"}
    assert all(
        candidate["operational_candidate_receipt"]["signature_fields"]["phase"] == "storage"
        for candidate in candidates
    )
    assert all(operational_candidate_receipt_errors(candidate) == [] for candidate in candidates)


def _problem_record(problem_id: str, atom_id: str) -> dict[str, object]:
    return {
        "problem_id": problem_id,
        "title": "The runner cannot start the automated stage",
        "problem": "A typed runner configuration failure prevents automated work.",
        "user_impact": "The requested backlog stage does not execute.",
        "severity": "high",
        "confidence": 0.95,
        "problem_status": "identified",
        "suggested_owner": "runner",
        "evidence_atom_ids": [atom_id],
    }


def _disk_signal_record(
    run_id: str,
    artifact_sha256: str,
    *,
    spelling_variant: bool = False,
) -> dict[str, object]:
    return _record(
        run_id,
        mission_id="implement_maintenance_backlog_ticket_v1",
        error={
            "type": "AgentExecFailed",
            "subtype": "disk_full",
            "exit_code": 1,
        },
        operational_failure_signals=[
            {
                "kind": "infrastructure",
                "phase": "storage",
                "prevented_stage": True,
                "error_type": "agent_exec_failed" if spelling_variant else "AgentExecFailed",
                "error_subtype": "DISK_FULL" if spelling_variant else "disk_full",
                "backend": "LOCAL" if spelling_variant else "local",
                "report_schema": (
                    "CONFIGS\\REPORT_SCHEMAS\\TASK.SCHEMA.JSON"
                    if spelling_variant
                    else "configs/report_schemas/task.schema.json"
                ),
                "artifact_sha256": artifact_sha256,
            }
        ],
    )


def test_artifact_hash_changes_occurrence_not_disk_full_signature_or_case() -> None:
    first_run = "implementation/run/disk-artifact-1"
    second_run = "implementation/run/disk-artifact-2"
    first_atom = _atom(
        first_run,
        f"{first_run}:run_failure_event:1",
        role="implementation",
    )
    second_atom = _atom(
        second_run,
        f"{second_run}:run_failure_event:1",
        role="implementation",
    )
    first_candidate = build_operational_failure_candidates(
        [_disk_signal_record(first_run, "a" * 64)],
        [first_atom],
    )[0]
    first_case = assign_problem_case_ids(
        [_problem_record("problem:disk:first", first_candidate["atom_id"])],
        [first_candidate],
    )[0]
    registry = build_case_registry([first_case], supporting_atoms=[first_candidate])

    expanded_candidate = build_operational_failure_candidates(
        [
            _disk_signal_record(first_run, "a" * 64),
            _disk_signal_record(second_run, "b" * 64, spelling_variant=True),
        ],
        [first_atom, second_atom],
    )[0]
    assert (
        expanded_candidate["operational_candidate_signature"]
        == first_candidate["operational_candidate_signature"]
    )
    assert expanded_candidate["atom_id"] != first_candidate["atom_id"]
    projected_hashes = {
        signal["failure_evidence_projection"]["signal"]["artifact_sha256"]
        for signal in expanded_candidate["operational_candidate_receipt"]["typed_signal_receipts"]
    }
    assert projected_hashes == {"a" * 64, "b" * 64}

    normalized = normalize_atom_lineage(
        [expanded_candidate],
        case_registry=registry,
        strict_new_output=True,
    )[0]
    recurrence = assign_problem_case_ids(
        [_problem_record("problem:disk:recurrence", normalized["atom_id"])],
        [normalized],
        case_registry=registry,
    )[0]
    assert recurrence["case_id"] == first_case["case_id"]


def test_broad_signature_after_split_stays_on_parent_pending_relation() -> None:
    first_run = "implementation/run/split-parent-1"
    second_run = "implementation/run/split-parent-2"
    first_atom = _atom(
        first_run,
        f"{first_run}:run_failure_event:1",
        role="implementation",
    )
    first_candidate = build_operational_failure_candidates(
        [_disk_signal_record(first_run, "a" * 64)],
        [first_atom],
    )[0]
    parent_case = assign_problem_case_ids(
        [_problem_record("problem:disk:parent", first_candidate["atom_id"])],
        [first_candidate],
    )[0]
    registry = build_case_registry([parent_case], supporting_atoms=[first_candidate])
    parent_case_id = parent_case["case_id"]
    registry["cases"][parent_case_id]["state"] = "split"
    registry["cases"][parent_case_id]["child_case_ids"] = ["case:child-a", "case:child-b"]

    second_atom = _atom(
        second_run,
        f"{second_run}:run_failure_event:1",
        role="implementation",
    )
    expanded = build_operational_failure_candidates(
        [
            _disk_signal_record(first_run, "a" * 64),
            _disk_signal_record(second_run, "b" * 64),
        ],
        [first_atom, second_atom],
    )[0]
    normalized = normalize_atom_lineage(
        [expanded],
        case_registry=registry,
        strict_new_output=True,
    )[0]

    [broad_recurrence] = assign_problem_case_ids(
        [_problem_record("problem:disk:broad-recurrence", expanded["atom_id"])],
        [normalized],
        case_registry=registry,
    )

    assert broad_recurrence["case_id"] == parent_case_id
    assert broad_recurrence["case_identity_status"] == "pending_relation"
    assert broad_recurrence["case_identity_candidate_ids"] == [
        "case:child-a",
        "case:child-b",
    ]
    assert broad_recurrence["related_case_ids"] == ["case:child-a", "case:child-b"]


def test_many_occurrences_keep_full_audit_ledger_and_bounded_prompt_projection() -> None:
    records: list[dict[str, object]] = []
    atoms: list[dict[str, object]] = []
    for index in range(400):
        run_id = f"implementation/run/disk-volume-{index:04d}"
        record = _disk_signal_record(run_id, f"{index:064x}")
        error = record["error"]
        assert isinstance(error, dict)
        error["agent"] = f"agent_{index:04d}"
        records.append(record)
        atoms.append(
            _atom(
                run_id,
                f"{run_id}:run_failure_event:1",
                role="implementation",
            )
        )

    candidate = build_operational_failure_candidates(records, atoms)[0]
    receipt = candidate["operational_candidate_receipt"]
    prompt_projection = candidate["operational_candidate_prompt_projection"]

    assert len(json.dumps(receipt, separators=(",", ":")).encode()) > 55_000
    assert len(json.dumps(prompt_projection, separators=(",", ":")).encode()) < 8_000
    assert len(candidate["text"].encode()) < 8_000
    assert receipt["occurrence_count"] == 400
    assert len(receipt["typed_signal_receipts"]) == 400
    assert prompt_projection["occurrence_count"] == 400
    assert prompt_projection["source_derived_atom_count"] == 400
    assert prompt_projection["evidence_shape_count"] == 400
    assert 0 < prompt_projection["evidence_shapes_included_count"] <= 8
    assert prompt_projection["evidence_shapes_omitted_count"] == (
        400 - prompt_projection["evidence_shapes_included_count"]
    )
    assert prompt_projection["full_receipt_sha256"] == receipt["receipt_sha256"]
    assert prompt_projection["full_occurrence_ledger"].endswith("excluded_from_stage1_prompt")
    assert operational_candidate_receipt_errors(candidate) == []


def test_occurrence_set_identity_changes_but_signature_reuses_and_reopens_case(
    tmp_path: Path,
) -> None:
    first_run = "research/run/occurrence-1"
    second_run = "research/run/occurrence-2"
    first_record = _record(first_run, error=_config_error())
    first_atom = _atom(first_run, f"{first_run}:run_failure_event:1")
    first_candidate = build_operational_failure_candidates(
        [first_record],
        [first_atom],
    )[0]
    unchanged_candidate = build_operational_failure_candidates(
        [deepcopy(first_record)],
        [deepcopy(first_atom)],
    )[0]

    assert unchanged_candidate["atom_id"] == first_candidate["atom_id"]
    assert (
        unchanged_candidate["operational_candidate_receipt"]["occurrence_set_sha256"]
        == first_candidate["operational_candidate_receipt"]["occurrence_set_sha256"]
    )

    first_case = assign_problem_case_ids(
        [_problem_record("problem:runner-config:first", first_candidate["atom_id"])],
        [first_candidate],
    )[0]
    registry = build_case_registry(
        [first_case],
        supporting_atoms=[first_candidate],
    )
    case_id = first_case["case_id"]
    signature = first_candidate["operational_candidate_signature"]
    assert registry["operational_signature_to_case_id"] == {signature: case_id}
    registry_path = tmp_path / "case_registry.json"
    write_case_registry(registry_path, registry)
    registry = load_case_registry(registry_path)
    assert registry["operational_signature_to_case_id"] == {signature: case_id}

    unchanged_normalized = normalize_atom_lineage(
        [unchanged_candidate],
        case_registry=registry,
        strict_new_output=True,
    )[0]
    assert unchanged_normalized["disposition"] == "supports_case"
    assert eligible_problem_mining_atoms([unchanged_normalized]) == []

    second_record = _record(second_run, error=_config_error())
    second_atom = _atom(second_run, f"{second_run}:run_failure_event:1")
    expanded_candidate = build_operational_failure_candidates(
        [first_record, second_record],
        [first_atom, second_atom],
    )[0]
    assert expanded_candidate["atom_id"] != first_candidate["atom_id"]
    assert expanded_candidate["operational_candidate_signature"] == signature
    assert expanded_candidate["operational_candidate_receipt"]["occurrence_count"] == 2

    expanded_normalized = normalize_atom_lineage(
        [expanded_candidate],
        case_registry=registry,
        strict_new_output=True,
    )[0]
    assert expanded_normalized["disposition"] == "unresolved"
    assert eligible_problem_mining_atoms([expanded_normalized]) == [expanded_normalized]

    recurrence_case = assign_problem_case_ids(
        [_problem_record("problem:runner-config:recurrence", expanded_candidate["atom_id"])],
        [expanded_normalized],
        case_registry=registry,
    )[0]
    assert recurrence_case["case_id"] == case_id

    registry["cases"][case_id]["state"] = "resolved"
    registry["cases"][case_id]["current_lifecycle"] = {
        "state": "resolved",
        "outcome_reference": {"plan_revision_id": "planrev:resolved"},
    }
    recurrence_case["case_state"] = "active"
    recurrence_case["reopened_from_state"] = "resolved"
    updated = build_case_registry(
        [recurrence_case],
        previous=registry,
        supporting_atoms=[expanded_normalized],
    )

    assert updated["operational_signature_to_case_id"] == {signature: case_id}
    assert updated["atom_id_to_case_id"][expanded_candidate["atom_id"]] == case_id
    assert updated["cases"][case_id]["state"] == "active"
    assert updated["cases"][case_id]["recurrence_reopen"] == {
        "from_state": "resolved",
        "against_plan_revision_id": "planrev:resolved",
        "case_revision": updated["cases"][case_id]["case_revision"],
        "new_evidence_atom_ids": [expanded_candidate["atom_id"]],
    }


def test_preflight_failures_collapse_exact_repeats_but_split_distinct_causes() -> None:
    gemini_error = {
        "type": "AgentPreflightFailed",
        "subtype": "mission_requires_shell",
        "code": "mission_requires_shell",
        "agent": "gemini",
        "capability": "shell_commands",
        "preflight": {
            "shell_capability": {
                "state": "blocked",
                "backend": "local",
                "reason_code": "gemini_sandbox_unavailable",
            }
        },
    }
    codex_error = {
        "type": "AgentPreflightFailed",
        "subtype": "mission_requires_shell",
        "code": "mission_requires_shell",
        "agent": "codex",
        "capability": "shell_commands",
        "preflight": {
            "shell_capability": {
                "state": "blocked",
                "backend": "docker",
                "reason_code": "shell_probe_failed",
                "policy_status": "allowed",
            }
        },
    }
    records = [
        *[
            _record(
                f"implementation/run/gemini-{index}",
                error=deepcopy(gemini_error),
                mission_id="implement_maintenance_backlog_ticket_v1",
            )
            for index in range(3)
        ],
        _record(
            "implementation/run/codex",
            error=codex_error,
            mission_id="implement_maintenance_backlog_ticket_v1",
        ),
    ]
    atoms = [
        _atom(
            record["run_rel"],
            f"{record['run_rel']}:run_failure_event:1",
            role="implementation",
        )
        for record in records
    ]

    candidates = build_operational_failure_candidates(records, atoms)

    assert len(candidates) == 2
    assert sorted(
        candidate["operational_candidate_receipt"]["occurrence_count"] for candidate in candidates
    ) == [1, 3]
    assert {candidate["operational_failure_class"] for candidate in candidates} == {
        "agent_preflight"
    }
    assert {candidate["operational_failure_phase"] for candidate in candidates} == {"preflight"}
    assert all(operational_candidate_receipt_errors(candidate) == [] for candidate in candidates)


def test_report_validation_candidates_preserve_safe_cause_and_split_distinct_mechanisms() -> None:
    missing_run = "research/run/report-missing-extension"
    mismatch_run = "research/run/report-case-mismatch"
    records = [
        _record(
            missing_run,
            status="report_validation_error",
            error=None,
            report_validation_errors=[
                "missing required extension backlog_repro_research",
                "details=private-user-value-must-never-enter-mining",
            ],
        ),
        _record(
            mismatch_run,
            status="report_validation_error",
            error=None,
            report_validation_errors=[
                "case_id does not match assignment: expected=case:secret got=case:other"
            ],
        ),
    ]
    atoms = [
        _atom(
            run_id,
            f"{run_id}:report_validation_error:1",
            source="report_validation_error",
        )
        for run_id in (missing_run, mismatch_run)
    ]

    candidates = build_operational_failure_candidates(records, atoms)

    assert len(candidates) == 2
    issues_by_code = {
        signal["validation_issues"][0]["code"]: candidate
        for candidate in candidates
        for signal in [
            candidate["operational_candidate_prompt_projection"]["evidence_shapes"][0]["signal"]
        ]
    }
    assert set(issues_by_code) == {
        "case_assignment_mismatch",
        "required_extension_missing",
    }
    missing = issues_by_code["required_extension_missing"]
    missing_issue = missing["operational_candidate_prompt_projection"]["evidence_shapes"][0][
        "signal"
    ]["validation_issues"][0]
    assert missing_issue == {
        "code": "required_extension_missing",
        "constraint": "required",
        "field": "backlog_repro_research",
    }
    combined_text = "\n".join(str(candidate["text"]) for candidate in candidates)
    assert "private-user-value" not in combined_text
    assert "case:secret" not in combined_text
    assert "case:other" not in combined_text
    assert all(operational_candidate_receipt_errors(candidate) == [] for candidate in candidates)


def test_report_validation_supplemental_prose_does_not_split_same_machine_cause() -> None:
    records: list[dict[str, object]] = []
    atoms: list[dict[str, object]] = []
    for index, private_detail in enumerate(("first-private-value", "second-private-value")):
        run_id = f"research/run/report-same-cause-{index}"
        records.append(
            _record(
                run_id,
                status="report_validation_error",
                error=None,
                report_validation_errors=[
                    "missing required extension backlog_repro_research",
                    f"details={private_detail}",
                ],
            )
        )
        atoms.append(
            _atom(
                run_id,
                f"{run_id}:report_validation_error:1",
                source="report_validation_error",
            )
        )

    candidates = build_operational_failure_candidates(records, atoms)

    assert len(candidates) == 1
    assert candidates[0]["operational_candidate_receipt"]["occurrence_count"] == 2
    assert "first-private-value" not in candidates[0]["text"]
    assert "second-private-value" not in candidates[0]["text"]
    assert operational_candidate_receipt_errors(candidates[0]) == []


def test_report_validation_prompt_details_are_bounded_without_losing_set_identity() -> None:
    run_id = "research/run/report-many-errors"
    errors = [f"$.extensions.field_{index:03d}: non-empty string required" for index in range(80)]
    errors.append("details=super-secret-" + "x" * 2_000)
    candidate = build_operational_failure_candidates(
        [
            _record(
                run_id,
                status="report_validation_error",
                error=None,
                report_validation_errors=errors,
            )
        ],
        [
            _atom(
                run_id,
                f"{run_id}:report_validation_error:1",
                source="report_validation_error",
            )
        ],
    )[0]

    signal = candidate["operational_candidate_prompt_projection"]["evidence_shapes"][0]["signal"]
    receipt_signal = candidate["operational_candidate_receipt"]["typed_signal_receipts"][0][
        "failure_evidence_projection"
    ]["signal"]
    assert signal["validation_source_error_count"] == 81
    assert signal["validation_errors_unscanned_count"] == 0
    assert signal["validation_issue_count"] == 80
    assert len(signal["validation_issues"]) == 16
    assert signal["validation_issues_omitted_count"] == 64
    assert "validation_issue_set_sha256" not in signal
    assert len(receipt_signal["validation_issue_set_sha256"]) == 64
    assert "super-secret" not in candidate["text"]
    assert len(candidate["text"].encode()) < 12_000
    assert operational_candidate_receipt_errors(candidate) == []


def test_report_validation_classification_scan_bound_is_explicit() -> None:
    run_id = "research/run/report-scan-bound"
    candidate = build_operational_failure_candidates(
        [
            _record(
                run_id,
                status="report_validation_error",
                error=None,
                report_validation_errors=[
                    "$.extensions.shell_capability: required for shell-backend preflight reports"
                    for _ in range(400)
                ],
            )
        ],
        [
            _atom(
                run_id,
                f"{run_id}:report_validation_error:1",
                source="report_validation_error",
            )
        ],
    )[0]

    signal = candidate["operational_candidate_prompt_projection"]["evidence_shapes"][0]["signal"]
    assert signal["validation_source_error_count"] == 400
    assert signal["validation_errors_unscanned_count"] == 144
    assert signal["validation_issue_count"] == 1
    assert signal["validation_issues"] == [
        {
            "code": "required_extension_missing",
            "constraint": "required",
            "path": "root.extensions.shell_capability",
        }
    ]
    assert operational_candidate_receipt_errors(candidate) == []


def test_ordinary_failed_experiment_does_not_create_candidate() -> None:
    run_id = "research/run/fail-first"
    record = _record(
        run_id,
        status="ok",
        error=None,
        report_status="partial",
        metrics={"commands_executed": 3, "commands_failed": 1},
    )
    atom = _atom(
        run_id,
        f"{run_id}:command_failure:1",
        source="command_failure",
        command="python -m pytest test_original_failure.py",
        exit_code=1,
    )

    assert build_operational_failure_candidates([record], [atom]) == []


def test_original_scenario_failure_stays_on_parent_and_is_not_candidate() -> None:
    run_id = "verification/run/original"
    record = _record(
        run_id,
        mission_id="review_backlog_implementation_pr_v1",
        error={"type": "AgentExecFailed"},
        ticket_ref={
            "verification_binding": {
                "case_id": "case:original",
                "outcome_role": "original_scenario",
            }
        },
    )
    atom = _atom(
        run_id,
        f"{run_id}:verification_observation:1",
        source="verification_observation",
        parent_case_id="case:original",
        role="verification",
        outcome_role="original_scenario",
        result="failed",
    )
    before = deepcopy(atom)

    assert build_operational_failure_candidates([record], [atom]) == []
    assert atom == before
    assert atom["disposition"] == "supports_case"
    assert atom["parent_case_id"] == "case:original"


def test_failed_original_scenario_outcome_record_is_not_candidate() -> None:
    run_id = "verification/run/outcome-record"
    record = _record(
        run_id,
        mission_id="review_backlog_implementation_pr_v1",
        error={"type": "AgentExecFailed"},
        outcome_record={
            "case_id": "case:original",
            "original_scenario_evidence": [{"kind": "runner_outcome_role", "result": "failed"}],
        },
    )
    atom = _atom(
        run_id,
        f"{run_id}:run_failure_event:1",
        parent_case_id="case:original",
        role="verification",
    )

    assert build_operational_failure_candidates([record], [atom]) == []


def test_unparented_typed_error_retains_unavailable_binding() -> None:
    run_id = "implementation/run/unparented"
    record = _record(
        run_id,
        mission_id="implement_maintenance_backlog_ticket_v1",
        error={"type": "TransportError", "code": "transport_stream_closed"},
    )
    atom = _atom(
        run_id,
        f"{run_id}:run_failure_event:1",
        parent_case_id=None,
        role="implementation",
    )

    candidates = build_operational_failure_candidates([record], [atom])

    assert len(candidates) == 1
    receipt = candidates[0]["operational_candidate_receipt"]
    assert receipt["parent_binding_statuses"] == ["unavailable"]
    assert receipt["related_parent_case_ids"] == []
    assert receipt["parent_bindings"][0]["case_ids"] == []
    assert operational_candidate_receipt_errors(candidates[0]) == []


def test_tampered_signal_and_receipt_are_rejected() -> None:
    run_id = "research/run/tamper"
    candidate = build_operational_failure_candidates(
        [_record(run_id, error=_config_error())],
        [_atom(run_id, f"{run_id}:run_failure_event:1")],
    )[0]
    tampered = deepcopy(candidate)
    tampered["operational_candidate_receipt"]["typed_signal_receipts"][0][
        "failure_evidence_projection"
    ]["error"]["subtype"] = "tampered_subtype"

    errors = operational_candidate_receipt_errors(tampered)

    assert "operational_candidate_signal_evidence_hash_mismatch:0" in errors
    assert "operational_candidate_signal_hash_mismatch:0" in errors
    assert "operational_candidate_occurrence_set_hash_mismatch" in errors
    assert "operational_candidate_receipt_hash_mismatch" in errors


def test_only_valid_operational_candidate_receipt_is_eligible_for_stage_one() -> None:
    run_id = "research/run/eligibility"
    candidate = build_operational_failure_candidates(
        [_record(run_id, error=_config_error())],
        [_atom(run_id, f"{run_id}:run_failure_event:1")],
    )[0]
    assert [atom["atom_id"] for atom in eligible_problem_mining_atoms([candidate])] == [
        candidate["atom_id"]
    ]

    tampered = deepcopy(candidate)
    tampered["operational_candidate_receipt"]["typed_signal_receipts"][0][
        "failure_evidence_projection"
    ]["error"]["code"] = "tampered_code"
    normalized = normalize_atom_lineage([tampered], strict_new_output=True)[0]

    assert normalized["disposition"] == "unresolved"
    assert normalized["disposition_status"] == "pending"
    assert normalized["case_id"] is None
    assert normalized["lineage_mining_blocker"] == "invalid_operational_candidate_receipt"
    assert any(
        error.startswith(
            "operational_candidate_integrity:operational_candidate_signal_evidence_hash_mismatch"
        )
        for error in normalized["lineage_validation_errors"]
    )
    assert eligible_problem_mining_atoms([normalized]) == []
    # Direct eligibility also rejects an unnormalized forged observation.
    assert eligible_problem_mining_atoms([tampered]) == []


def test_proposals_and_derived_prose_are_excluded_from_candidate_evidence() -> None:
    run_id = "research/run/proposals"
    failure_id = f"{run_id}:run_failure_event:1"
    proposal_id = f"{run_id}:suggested_change:1"
    prose_id = f"{run_id}:confusion_point:1"
    atoms = [
        _atom(run_id, failure_id),
        _atom(
            run_id,
            proposal_id,
            source="suggested_change",
            evidence_class="proposal",
        ),
        _atom(run_id, prose_id, source="confusion_point"),
    ]

    candidate = build_operational_failure_candidates(
        [_record(run_id, error=_config_error())],
        atoms,
    )[0]
    receipt = candidate["operational_candidate_receipt"]

    assert receipt["source_derived_atom_ids"] == [failure_id]
    assert receipt["excluded_proposal_atom_ids"] == [proposal_id]
    assert receipt["excluded_context_atom_ids"] == [prose_id]
    assert candidate["derived_from_atom_ids"] == [failure_id]
    assert candidate["text"].startswith("Automated stage blocker:")
    assert "Free-form derived text" not in candidate["text"]
    assert operational_candidate_receipt_errors(candidate) == []


def test_bare_nonterminal_and_stderr_warning_do_not_create_candidates() -> None:
    run_id = "research/run/nonterminal"
    record = _record(
        run_id,
        status="nonterminal",
        error={"type": "AgentExecFailed"},
    )
    atom = _atom(
        run_id,
        f"{run_id}:agent_stderr_artifact:1",
        source="agent_stderr_artifact",
    )

    assert build_operational_failure_candidates([record], [atom]) == []


def test_typed_policy_block_can_create_candidate_but_report_failure_alone_cannot() -> None:
    policy_run = "research/run/policy"
    prose_run = "research/run/report-prose"
    records = [
        _record(
            policy_run,
            status="ok",
            error=None,
            report_status="failure",
            metrics={"commands_blocked_by_policy": 3},
        ),
        _record(
            prose_run,
            status="ok",
            error=None,
            report_status="failure",
            metrics={"commands_failed": 3},
        ),
    ]
    atoms = [
        _atom(policy_run, f"{policy_run}:report_outcome:1", source="report_outcome"),
        _atom(prose_run, f"{prose_run}:report_outcome:1", source="report_outcome"),
    ]

    candidates = build_operational_failure_candidates(records, atoms)

    assert len(candidates) == 1
    assert candidates[0]["operational_failure_class"] == "policy"
    assert operational_candidate_receipt_errors(candidates[0]) == []
