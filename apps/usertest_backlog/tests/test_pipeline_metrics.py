from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from reporter.materialize import materialize_lifecycle_metrics
from run_artifacts.lifecycle_events import (
    LifecycleContext,
    ModelUsageReceipt,
    append_lifecycle_event,
    canonical_sha256,
    make_lifecycle_event,
    write_content_addressed_model_usage_receipt,
)

from usertest_backlog.pipeline_metrics import (
    _cycle_for,
    bind_ticket_lifecycle_ids,
    case_lifecycle_id,
    record_stage_telemetry,
)


def _with_model_contract(
    stage: dict[str, object],
    *,
    manifest_paths: list[Path] | None = None,
    invocation_expected: bool = False,
) -> dict[str, object]:
    paths = manifest_paths or []
    contract: dict[str, object] = {
        "schema_version": 1,
        "agent": "codex",
        "dry_run": False,
        "subscription_required": True,
        "invocation_expected": invocation_expected,
        "manifests": [
            {
                "path": str(path),
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
            for path in paths
        ],
    }
    contract["contract_sha256"] = canonical_sha256(contract)
    updated = dict(stage)
    input_meta = dict(updated.get("input_meta") or {})
    input_meta["model_invocation_contract"] = contract
    updated["input_meta"] = input_meta
    return updated


def _write_research_model_run(
    root: Path,
    *,
    invocation_id: str,
    started_at: str,
    ended_at: str,
    total_tokens: int = 10,
    session_id: str = "session-1",
    source_case_id: str | None = None,
    source_lifecycle_id: str | None = None,
) -> Path:
    root.mkdir(parents=True)
    manifest_path = root / "research.model_invocation.json"
    manifest_path.write_text(json.dumps({"invocation_id": invocation_id}), encoding="utf-8")
    context = LifecycleContext(
        case_lifecycle_id=source_lifecycle_id,
        case_id=source_case_id,
        cycle_id="source-cycle",
        work_unit_id=f"model-work:{invocation_id}",
        invocation_id=invocation_id,
        session_id=session_id,
    )
    append_lifecycle_event(
        root / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "model.invocation.started",
            context,
            occurred_at=started_at,
            started_at=started_at,
        ),
    )
    append_lifecycle_event(
        root / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "model.invocation.completed",
            context,
            occurred_at=ended_at,
            started_at=started_at,
            ended_at=ended_at,
            active_seconds=1,
            attributes={
                "token_usage": {
                    "total_tokens": total_tokens,
                    "input_tokens": total_tokens - 1,
                    "cached_input_tokens": 2,
                    "uncached_input_tokens": total_tokens - 3,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 1,
                },
                "token_scope": "qualification",
                "cost_scope": "direct",
            },
        ),
    )
    (root / "run_meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_started_utc": started_at,
                "run_finished_utc": ended_at,
            }
        ),
        encoding="utf-8",
    )
    (root / "report.json").write_text("{}\n", encoding="utf-8")
    return manifest_path


def _write_historical_cumulative_research_run(
    root: Path,
    *,
    invocation_id: str,
    started_at: str,
    ended_at: str,
    usage: dict[str, int],
    session_id: str,
    continued_session: bool,
) -> tuple[Path, str, Path]:
    root.mkdir(parents=True)
    context = LifecycleContext(
        cycle_id="source-cycle",
        work_unit_id=f"model-work:{invocation_id}",
        invocation_id=invocation_id,
        session_id=session_id,
    )
    receipt = ModelUsageReceipt(
        receipt_id=f"usage:{invocation_id}",
        context=context,
        provider="codex",
        model="gpt-5.6",
        usage_semantics="per_invocation",
        recorded_at=ended_at,
        invocation_started_at=started_at,
        invocation_ended_at=ended_at,
        input_tokens=usage["input_tokens"],
        cached_input_tokens=usage["cached_input_tokens"],
        uncached_input_tokens=usage["uncached_input_tokens"],
        output_tokens=usage["output_tokens"],
        reasoning_tokens=usage["reasoning_output_tokens"],
        total_tokens=usage["total_tokens"],
        observed_usage={
            "total_tokens": usage["total_tokens"],
            "input_tokens": usage["input_tokens"],
            "cached_input_tokens": usage["cached_input_tokens"],
            "uncached_input_tokens": usage["uncached_input_tokens"],
            "output_tokens": usage["output_tokens"],
            "reasoning_tokens": usage["reasoning_output_tokens"],
        },
        provenance_quality="authoritative",
    )
    receipt_path = write_content_addressed_model_usage_receipt(
        root / "model_usage_receipts",
        receipt,
    )
    append_lifecycle_event(
        root / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "model.invocation.started",
            context,
            occurred_at=started_at,
            started_at=started_at,
        ),
    )
    completed = make_lifecycle_event(
        "model.invocation.completed",
        context,
        occurred_at=ended_at,
        started_at=started_at,
        ended_at=ended_at,
        active_seconds=1,
        evidence_paths=(str(receipt_path),),
        attributes={
            "token_usage": usage,
            "token_scope": "qualification",
            "cost_scope": "direct",
            "usage_semantics": "per_invocation",
            "continued_session": continued_session,
            "usage_receipt_path": str(receipt_path),
        },
    )
    append_lifecycle_event(root / "lifecycle_events.jsonl", completed)
    (root / "run_meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_started_utc": started_at,
                "run_finished_utc": ended_at,
            }
        ),
        encoding="utf-8",
    )
    (root / "report.json").write_text("{}\n", encoding="utf-8")
    return root / "lifecycle_events.jsonl", completed.event_id, receipt_path


def test_stage_telemetry_closes_verified_negative_and_is_idempotent(tmp_path: Path) -> None:
    registry_path = tmp_path / "case_registry.json"
    registry = {
        "cases": {
            "case-1": {
                "case_id": "case-1",
                "state": "active",
                "current_research_proof": {
                    "actionability_assessment": {"disposition": "already_addressed"}
                },
            }
        }
    }
    stage = {
        "stage": "ticket_assembly",
        "generated_at": "2026-07-21T12:00:00Z",
        "input_meta": {},
        "artifacts": {"backlog_json": "backlog.json"},
        "items": [{"case_id": "case-1", "ticket_stage": "not_emitted"}],
    }
    record_stage_telemetry(
        case_registry=registry,
        case_registry_path=registry_path,
        stage_doc=stage,
    )
    record_stage_telemetry(
        case_registry=registry,
        case_registry_path=registry_path,
        stage_doc=stage,
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event_type"] for row in rows].count("disposition.verified") == 1
    disposition = next(row for row in rows if row["event_type"] == "disposition.verified")
    assert disposition["attributes"]["disposition"] == "already_addressed"
    assert disposition["origin"] == "unknown_external"
    assert list((tmp_path / "telemetry" / "cases").rglob("lifecycle_manifest.json"))
    assert (tmp_path / "case_metrics.json").is_file()
    assert (tmp_path / "cohort_metrics.json").is_file()
    work = next(row for row in rows if row["event_type"] == "work.completed")
    assert work["attributes"]["cost_unknown"] is True
    assert work["attributes"]["model_usage_telemetry_complete"] is False
    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    [case] = metrics["cases"]
    assert case["accounting"]["inclusive"]["gross"]["total_tokens"] is None
    assert case["accounting"]["inclusive"]["gross"]["active_seconds"] is None


def test_shared_stage_and_ticket_binding_share_one_lifecycle_attempt(tmp_path: Path) -> None:
    registry_path = tmp_path / "case_registry.json"
    stage = {
        "stage": "problem_prioritization",
        "generated_at": "2026-07-21T12:00:00Z",
        "input_meta": {},
        "items": [{"case_id": "case-1"}, {"case_id": "case-2"}],
    }
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=registry_path,
        stage_doc=stage,
    )
    bound = bind_ticket_lifecycle_ids(
        [{"case_id": "case-1"}],
        case_registry_path=registry_path,
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    work = next(row for row in rows if row["event_type"] == "work.completed")
    assert len(work["beneficiary_case_lifecycle_ids"]) == 2
    case_stage = next(
        row
        for row in rows
        if row["event_type"] == "stage.completed" and row["context"]["case_id"] == "case-1"
    )
    assert bound[0]["case_lifecycle_id"] == case_stage["context"]["case_lifecycle_id"]


def test_stage_links_bound_model_usage_without_duplicating_case_cost(tmp_path: Path) -> None:
    invocation_dir = tmp_path / "invocation"
    invocation_dir.mkdir()
    invocation_id = "invocation-1"
    manifest_path = invocation_dir / "stage.model_invocation.json"
    manifest_path.write_text(json.dumps({"invocation_id": invocation_id}), encoding="utf-8")
    append_lifecycle_event(
        invocation_dir / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "model.invocation.completed",
            LifecycleContext(cycle_id="source-cycle", invocation_id=invocation_id),
            occurred_at="2026-07-21T12:00:05Z",
            active_seconds=5,
            attributes={
                "token_usage": {
                    "total_tokens": 12,
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "uncached_input_tokens": 8,
                    "output_tokens": 2,
                    "reasoning_output_tokens": 1,
                }
            },
        ),
    )
    append_lifecycle_event(
        invocation_dir / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "intervention.completed",
            LifecycleContext(cycle_id="source-cycle"),
            occurred_at="2026-07-21T12:00:06Z",
            actor_type="supervising_agent",
            initiator_type="supervising_agent",
            root_initiator_type="supervising_agent",
            origin="supervising_agent",
            intervention_id="supervisor-1",
            attributes={"required_for_progress": True},
        ),
    )
    stage = _with_model_contract(
        {
            "stage": "problem_mining",
            "generated_at": "2026-07-21T12:00:10Z",
            "input_meta": {},
            "items": [{"case_id": "case-1"}, {"case_id": "case-2"}],
        },
        manifest_paths=[manifest_path],
        invocation_expected=True,
    )
    registry_path = tmp_path / "case_registry.json"
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=registry_path,
        stage_doc=stage,
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    linked = [row for row in rows if row["event_type"] == "model.invocation.completed"]
    assert len(linked) == 1
    assert linked[0]["attributes"]["token_usage"]["total_tokens"] == 12
    assert len(linked[0]["beneficiary_case_lifecycle_ids"]) == 2
    stage_work = next(row for row in rows if row["event_type"] == "work.completed")
    assert linked[0]["context"]["work_unit_id"] != stage_work["context"]["work_unit_id"]
    assert linked[0]["attributes"]["stage_work_unit_id"] == stage_work["context"]["work_unit_id"]
    interventions = [row for row in rows if row["event_type"] == "intervention.completed"]
    assert len(interventions) == 1
    assert len(interventions[0]["beneficiary_case_lifecycle_ids"]) == 2
    assert interventions[0]["context"]["work_unit_id"] != linked[0]["context"]["work_unit_id"]
    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    assert metrics["reconciliation"]["ok"] is False
    assert {issue["code"] for issue in metrics["reconciliation"]["issues"]} == {
        "supervising_agent_tokens_missing",
        "work_unit_resource_time_unknown",
    }
    assert {
        case["accounting"]["inclusive"]["gross"]["total_tokens"] for case in metrics["cases"]
    } == {12}


def test_stage_link_rehomes_complete_runner_boundary_to_canonical_lifecycle(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "research-run"
    source_lifecycle_id = "case-lifecycle:source-run"
    manifest_path = _write_research_model_run(
        run_dir,
        invocation_id="invocation-1",
        started_at="2026-07-21T12:00:01Z",
        ended_at="2026-07-21T12:00:05Z",
        total_tokens=10,
        source_case_id="case-1",
        source_lifecycle_id=source_lifecycle_id,
    )
    runner_context = LifecycleContext(
        case_lifecycle_id=source_lifecycle_id,
        case_id="case-1",
        cycle_id="source-cycle",
        stage="repro_research",
        work_unit_id="runner-work:1",
    )
    append_lifecycle_event(
        run_dir / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "work.created",
            runner_context,
            occurred_at="2026-07-21T12:00:00Z",
            started_at="2026-07-21T12:00:00Z",
            attributes={"scope": "pipeline"},
        ),
    )
    append_lifecycle_event(
        run_dir / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "work.completed",
            runner_context,
            occurred_at="2026-07-21T12:00:06Z",
            started_at="2026-07-21T12:00:00Z",
            ended_at="2026-07-21T12:00:06Z",
            attributes={
                "scope": "pipeline",
                "wall_clock_envelope_seconds": 6,
            },
        ),
    )
    registry_path = tmp_path / "case_registry.json"
    stage = _with_model_contract(
        {
            "stage": "repro_research",
            "generated_at": "2026-07-21T12:00:10Z",
            "input_meta": {},
            "items": [
                {
                    "case_id": "case-1",
                    "research_attempts": [
                        {
                            "attempt_number": 1,
                            "attempt_kind": "full_research",
                            "outcome": "output_contract_valid",
                            "run_dir": str(run_dir),
                            "report_path": str(run_dir / "report.json"),
                            "validation_errors": [],
                        }
                    ],
                }
            ],
        },
        manifest_paths=[manifest_path],
        invocation_expected=True,
    )
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=registry_path,
        stage_doc=stage,
    )
    merged_dir = tmp_path / "merged"
    materialize_lifecycle_metrics(
        event_sources=[
            run_dir / "lifecycle_events.jsonl",
            tmp_path / "lifecycle_events.jsonl",
        ],
        output_dir=merged_dir,
    )

    metrics = json.loads((merged_dir / "case_metrics.json").read_text(encoding="utf-8"))
    assert [case["case_id"] for case in metrics["cases"]] == ["case-1"]
    [case] = metrics["cases"]
    assert case["case_lifecycle_id"] != source_lifecycle_id
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 10
    assert "runner-work:1" in case["accounting"]["direct"]["work_unit_ids"]
    linked_types = {
        row["event_type"]
        for row in (
            json.loads(line)
            for line in (tmp_path / "lifecycle_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        if row.get("attributes", {}).get("linked_source_event_id")
    }
    assert {
        "work.created",
        "work.completed",
        "model.invocation.started",
        "model.invocation.completed",
    }.issubset(linked_types)


def test_reused_stage_without_complete_prior_work_edges_withholds_cost(tmp_path: Path) -> None:
    stage = {
        "stage": "repro_research",
        "generated_at": "2026-07-21T12:00:00Z",
        "input_meta": {"retained_research_reused_count": 1},
        "items": [{"case_id": "case-1"}],
    }
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=tmp_path / "case_registry.json",
        stage_doc=stage,
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    reused = next(row for row in rows if row["event_type"] == "work.reused")
    assert reused["attributes"]["cost_unknown"] is True
    assert reused["attributes"]["dependency_ids"] == []

    case = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))["cases"][0]
    assert case["accounting"]["inclusive"]["gross"]["total_tokens"] is None
    assert case["accounting"]["inclusive"]["gross"]["active_seconds"] is None
    assert case["reconciliation"]["ok"] is False
    assert any(
        issue["code"] == "work_unit_cost_unknown" for issue in case["reconciliation"]["issues"]
    )


def test_reused_stage_accepts_explicit_complete_prior_work_edges(tmp_path: Path) -> None:
    stage = _with_model_contract(
        {
            "stage": "repro_research",
            "generated_at": "2026-07-21T12:00:00Z",
            "input_meta": {
                "retained_research_reused_count": 1,
                "prior_work_unit_ids": ["prior:stage1", "prior:stage2"],
                "reused_work_dependency_set_complete": True,
            },
            "items": [{"case_id": "case-1"}],
        }
    )
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=tmp_path / "case_registry.json",
        stage_doc=stage,
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    reused = next(row for row in rows if row["event_type"] == "work.reused")
    assert reused["attributes"]["cost_unknown"] is False
    assert reused["attributes"]["dependency_ids"] == [
        "prior:stage1",
        "prior:stage2",
    ]


def test_stage_derives_raw_atom_boundary_from_retained_origin_id(tmp_path: Path) -> None:
    stage = {
        "stage": "problem_mining",
        "generated_at": "2026-07-21T12:00:00Z",
        "input_meta": {},
        "items": [
            {
                "case_id": "case-1",
                "evidence_atom_ids": ["target/20260719T083852Z/codex/1:confusion_point:1"],
            }
        ],
    }
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=tmp_path / "case_registry.json",
        stage_doc=stage,
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    opened = next(row for row in rows if row["event_type"] == "lifecycle.opened")
    assert opened["attributes"]["origin_ids"] == [
        "target/20260719T083852Z/codex/1:confusion_point:1"
    ]
    assert opened["attributes"]["atom_created_at"] == "2026-07-19T08:38:52Z"


def test_stage3_projects_run_receipts_and_validation_self_healing(
    tmp_path: Path,
) -> None:
    runs = [tmp_path / f"run-{index}" for index in range(1, 5)]
    boundaries = [
        ("2026-07-21T12:00:01Z", "2026-07-21T12:00:10Z"),
        ("2026-07-21T12:00:11Z", "2026-07-21T12:00:20Z"),
        ("2026-07-21T12:00:31Z", "2026-07-21T12:00:40Z"),
        ("2026-07-21T12:00:41Z", "2026-07-21T12:00:50Z"),
    ]
    for index, (run_dir, (started_at, ended_at)) in enumerate(
        zip(runs, boundaries, strict=True), start=1
    ):
        _write_research_model_run(
            run_dir,
            invocation_id=f"invocation-{index}",
            started_at=started_at,
            ended_at=ended_at,
        )

    def attempt(
        number: int,
        kind: str,
        run_dir: Path,
        errors: list[str],
        *,
        resumed: bool,
    ) -> dict[str, object]:
        return {
            "attempt_number": number,
            "attempt_kind": kind,
            "outcome": "repair_contract_valid" if not errors else "repair_contract_invalid",
            "run_dir": str(run_dir),
            "report_path": str(run_dir / "report.json"),
            "validation_errors": errors,
            "agent_session_id": "session-1",
            "observed_agent_session_id": "session-1",
            "resumed_from_session_id": "session-1" if resumed else None,
        }

    stage = {
        "stage": "repro_research",
        "generated_at": "2026-07-21T12:01:00Z",
        "input_meta": {},
        "items": [
            {
                "case_id": "case-1",
                "research_attempts": [
                    attempt(
                        1,
                        "full_research",
                        runs[0],
                        [
                            'output_rule:field:details={"value":1}',
                            'output_rule:field:details={"value":1}',
                        ],
                        resumed=False,
                    ),
                    attempt(2, "model_output_repair", runs[1], [], resumed=True),
                    attempt(
                        3,
                        "evidence_verification_feedback",
                        runs[0],
                        ['binding_rule:atom:details={"candidate":"missing"}'],
                        resumed=False,
                    ),
                    attempt(
                        4,
                        "evidence_verification_research_continuation",
                        runs[2],
                        ['binding_rule:atom:details={"candidate":"wrong"}'],
                        resumed=True,
                    ),
                    attempt(
                        5,
                        "evidence_verification_research_continuation",
                        runs[3],
                        [],
                        resumed=True,
                    ),
                ],
            }
        ],
    }
    registry_path = tmp_path / "case_registry.json"
    for _ in range(2):
        record_stage_telemetry(
            case_registry={"cases": {}},
            case_registry_path=registry_path,
            stage_doc=stage,
        )

    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event_type"] for row in rows].count("model.invocation.completed") == 4
    assert [row["event_type"] for row in rows].count("error.occurred") == 4
    assert [row["event_type"] for row in rows].count("error.resolved") == 2
    work = next(row for row in rows if row["event_type"] == "work.completed")
    assert work["attributes"]["model_usage_telemetry_complete"] is True
    assert work["attributes"]["cost_unknown"] is False

    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    [case] = metrics["cases"]
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 40
    assert case["errors"]["cluster_count"] == 2
    assert case["errors"]["occurrence_count"] == 4
    assert case["errors"]["self_healed_cluster_count"] == 2

    identity_by_cluster = {
        row["error_cluster_id"]: row["attributes"]["validation_error_identity"]
        for row in rows
        if row["event_type"] == "error.occurred"
    }
    clusters = {
        identity_by_cluster[cluster["error_cluster_id"]]: cluster
        for cluster in case["errors"]["clusters"]
    }
    assert clusters["output_rule:field"]["resolution_total_tokens"] == 10
    assert clusters["output_rule:field"]["resolution_elapsed_seconds"] == 10
    assert clusters["output_rule:field"]["resolution_timing_complete"] is True
    assert clusters["binding_rule:atom"]["resolution_total_tokens"] == 20
    assert clusters["binding_rule:atom"]["resolution_elapsed_seconds"] is None
    assert clusters["binding_rule:atom"]["resolution_timing_complete"] is False


def test_stage3_model_cost_is_bound_to_each_case_not_shared_across_cohort(
    tmp_path: Path,
) -> None:
    first_run = tmp_path / "first-run"
    second_run = tmp_path / "second-run"
    _write_research_model_run(
        first_run,
        invocation_id="invocation-first",
        started_at="2026-07-21T12:00:01Z",
        ended_at="2026-07-21T12:00:02Z",
        total_tokens=10,
    )
    _write_research_model_run(
        second_run,
        invocation_id="invocation-second",
        started_at="2026-07-21T12:00:03Z",
        ended_at="2026-07-21T12:00:04Z",
        total_tokens=20,
        session_id="session-2",
    )
    stage = {
        "stage": "repro_research",
        "generated_at": "2026-07-21T12:00:05Z",
        "input_meta": {},
        "items": [
            {
                "case_id": "case-1",
                "research_attempts": [
                    {
                        "attempt_number": 1,
                        "attempt_kind": "full_research",
                        "outcome": "output_contract_valid",
                        "run_dir": str(first_run),
                        "report_path": str(first_run / "report.json"),
                        "validation_errors": [],
                        "agent_session_id": "session-1",
                        "observed_agent_session_id": "session-1",
                    }
                ],
            },
            {
                "case_id": "case-2",
                "research_attempts": [
                    {
                        "attempt_number": 1,
                        "attempt_kind": "full_research",
                        "outcome": "output_contract_valid",
                        "run_dir": str(second_run),
                        "report_path": str(second_run / "report.json"),
                        "validation_errors": [],
                        "agent_session_id": "session-2",
                        "observed_agent_session_id": "session-2",
                    }
                ],
            },
        ],
    }
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=tmp_path / "case_registry.json",
        stage_doc=stage,
    )

    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    direct_tokens = {
        case["case_id"]: case["accounting"]["direct"]["gross"]["total_tokens"]
        for case in metrics["cases"]
    }
    assert direct_tokens == {"case-1": 10, "case-2": 20}
    assert {
        case["case_id"]: case["accounting"]["inclusive"]["gross"]["total_tokens"]
        for case in metrics["cases"]
    } == direct_tokens


def test_stage3_corrects_retained_codex_session_high_water_without_rewriting(
    tmp_path: Path,
) -> None:
    session_id = "session-cumulative"
    first_usage = {
        "total_tokens": 120,
        "input_tokens": 100,
        "cached_input_tokens": 30,
        "uncached_input_tokens": 70,
        "output_tokens": 20,
        "reasoning_output_tokens": 10,
    }
    second_usage = {
        "total_tokens": 177,
        "input_tokens": 150,
        "cached_input_tokens": 50,
        "uncached_input_tokens": 100,
        "output_tokens": 27,
        "reasoning_output_tokens": 12,
    }
    first_run = tmp_path / "first-run"
    second_run = tmp_path / "second-run"
    first_log, first_source_event_id, first_receipt = _write_historical_cumulative_research_run(
        first_run,
        invocation_id="invocation-first",
        started_at="2026-07-21T12:00:01Z",
        ended_at="2026-07-21T12:00:02Z",
        usage=first_usage,
        session_id=session_id,
        continued_session=False,
    )
    second_log, second_source_event_id, second_receipt = _write_historical_cumulative_research_run(
        second_run,
        invocation_id="invocation-second",
        started_at="2026-07-21T12:00:03Z",
        ended_at="2026-07-21T12:00:04Z",
        usage=second_usage,
        session_id=session_id,
        continued_session=True,
    )
    registry_path = tmp_path / "case_registry.json"
    lifecycle_id = case_lifecycle_id(
        case_registry_path=registry_path,
        case_id="case-1",
    )
    cycle = _cycle_for(registry_path)
    global_path = tmp_path / "lifecycle_events.jsonl"
    for invocation_id, source_event_id, source_log, receipt_path, usage, occurred_at in (
        (
            "invocation-first",
            first_source_event_id,
            first_log,
            first_receipt,
            first_usage,
            "2026-07-21T12:00:02Z",
        ),
        (
            "invocation-second",
            second_source_event_id,
            second_log,
            second_receipt,
            second_usage,
            "2026-07-21T12:00:04Z",
        ),
    ):
        append_lifecycle_event(
            global_path,
            make_lifecycle_event(
                "model.invocation.completed",
                LifecycleContext(
                    case_lifecycle_id=lifecycle_id,
                    case_id="case-1",
                    cycle_id=cycle.cycle_id,
                    stage="repro_research",
                    milestone_id="stage3",
                    work_unit_id=f"model-work:{invocation_id}",
                    invocation_id=invocation_id,
                    session_id=session_id,
                ),
                idempotency_key=(f"linked:{cycle.cycle_id}:{source_event_id}"),
                occurred_at=occurred_at,
                active_seconds=1,
                beneficiary_case_lifecycle_ids=(lifecycle_id,),
                evidence_paths=(str(receipt_path),),
                attributes={
                    "token_usage": usage,
                    "token_scope": "qualification",
                    "cost_scope": "direct",
                    "usage_semantics": "per_invocation",
                    "linked_source_event_id": source_event_id,
                    "linked_source_event_log": str(source_log),
                },
            ),
        )
    stage = {
        "stage": "repro_research",
        "generated_at": "2026-07-21T12:00:05Z",
        "input_meta": {},
        "items": [
            {
                "case_id": "case-1",
                "research_attempts": [
                    {
                        "attempt_number": 1,
                        "attempt_kind": "full_research",
                        "outcome": "output_contract_valid",
                        "run_dir": str(first_run),
                        "report_path": str(first_run / "report.json"),
                        "validation_errors": [],
                        "agent_session_id": session_id,
                        "observed_agent_session_id": session_id,
                    },
                    {
                        "attempt_number": 2,
                        "attempt_kind": "targeted_repair",
                        "outcome": "output_contract_valid",
                        "run_dir": str(second_run),
                        "report_path": str(second_run / "report.json"),
                        "validation_errors": [],
                        "agent_session_id": session_id,
                        "observed_agent_session_id": session_id,
                    },
                ],
            }
        ],
    }
    for _ in range(2):
        record_stage_telemetry(
            case_registry={"cases": {}},
            case_registry_path=registry_path,
            stage_doc=stage,
        )

    rows = [json.loads(line) for line in global_path.read_text(encoding="utf-8").splitlines()]
    corrections = [row for row in rows if row["event_type"] == "model.usage.corrected"]
    assert len(corrections) == 1
    assert corrections[0]["attributes"]["corrected_token_usage"] == {
        "total_tokens": 57,
        "input_tokens": 50,
        "cached_input_tokens": 20,
        "uncached_input_tokens": 30,
        "output_tokens": 7,
        "reasoning_output_tokens": 2,
    }
    retained_second = next(
        row
        for row in rows
        if row["event_type"] == "model.invocation.completed"
        and row["attributes"].get("linked_source_event_id") == second_source_event_id
    )
    assert retained_second["attributes"]["token_usage"]["total_tokens"] == 177
    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    [case] = metrics["cases"]
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 177
    assert metrics["normalization"]["model_usage_corrections"][0]["token_usage_complete"] is True


def test_stage3_checkpoint_projects_committed_case_without_completing_batch(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "research-run"
    _write_research_model_run(
        run_dir,
        invocation_id="invocation-checkpoint",
        started_at="2026-07-21T12:00:01Z",
        ended_at="2026-07-21T12:00:10Z",
        total_tokens=10,
    )
    item = {
        "case_id": "case-1",
        "research_attempts": [
            {
                "attempt_number": 1,
                "attempt_kind": "full_research",
                "outcome": "output_contract_valid",
                "run_dir": str(run_dir),
                "report_path": str(run_dir / "report.json"),
                "validation_errors": [],
                "agent_session_id": "session-1",
                "observed_agent_session_id": "session-1",
            }
        ],
    }
    checkpoint = {
        "stage": "repro_research",
        "generated_at": "2026-07-21T12:00:30Z",
        "input_meta": {"stage_status": "checkpointed_progress"},
        "items": [item],
    }
    registry_path = tmp_path / "case_registry.json"
    for _ in range(2):
        record_stage_telemetry(
            case_registry={"cases": {}},
            case_registry_path=registry_path,
            stage_doc=checkpoint,
        )

    def rows() -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in (tmp_path / "lifecycle_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    retained = rows()
    assert [row["event_type"] for row in retained].count("stage.checkpointed") == 1
    assert [row["event_type"] for row in retained].count("work.completed") == 0
    assert [row["event_type"] for row in retained].count("stage.completed") == 1
    stage_completed = next(row for row in retained if row["event_type"] == "stage.completed")
    assert stage_completed["occurred_at"] == "2026-07-21T12:00:10Z"
    assert stage_completed["attributes"]["completion_scope"] == ("committed_case_prefix")
    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    [case] = metrics["cases"]
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 10
    assert case["lifecycle_status"] == "active"

    completed = {
        **checkpoint,
        "generated_at": "2026-07-21T12:01:00Z",
        "input_meta": {"stage_status": "completed"},
    }
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=registry_path,
        stage_doc=completed,
    )
    retained = rows()
    assert [row["event_type"] for row in retained].count("work.completed") == 1
    assert [row["event_type"] for row in retained].count("stage.completed") == 1
    assert [row["event_type"] for row in retained].count("model.invocation.completed") == 1
    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    [case] = metrics["cases"]
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 10


def test_stage3_checkpoint_preserves_legacy_stage_completion_idempotency(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "case_registry.json"
    lifecycle_id = case_lifecycle_id(
        case_registry_path=registry_path,
        case_id="case-1",
    )
    append_lifecycle_event(
        tmp_path / "lifecycle_events.jsonl",
        make_lifecycle_event(
            "stage.completed",
            LifecycleContext(
                case_lifecycle_id=lifecycle_id,
                case_id="case-1",
                stage="repro_research",
                milestone_id="research_completed",
            ),
            idempotency_key="legacy-stage-completion-key",
            occurred_at="2026-07-21T12:00:10Z",
            attributes={"stage": "repro_research"},
        ),
    )
    record_stage_telemetry(
        case_registry={"cases": {}},
        case_registry_path=registry_path,
        stage_doc={
            "stage": "repro_research",
            "generated_at": "2026-07-21T12:00:30Z",
            "input_meta": {"stage_status": "checkpointed_progress"},
            "items": [{"case_id": "case-1", "research_attempts": []}],
        },
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event_type"] for row in rows].count("stage.completed") == 1
