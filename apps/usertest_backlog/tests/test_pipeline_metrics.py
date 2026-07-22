from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from run_artifacts.lifecycle_events import (
    LifecycleContext,
    append_lifecycle_event,
    canonical_sha256,
    make_lifecycle_event,
)

from usertest_backlog.pipeline_metrics import (
    bind_ticket_lifecycle_ids,
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
    manifest_path.write_text(
        json.dumps({"invocation_id": invocation_id}), encoding="utf-8"
    )
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
    assert linked[0]["attributes"]["stage_work_unit_id"] == stage_work["context"][
        "work_unit_id"
    ]
    interventions = [row for row in rows if row["event_type"] == "intervention.completed"]
    assert len(interventions) == 1
    assert len(interventions[0]["beneficiary_case_lifecycle_ids"]) == 2
    assert interventions[0]["context"]["work_unit_id"] != linked[0]["context"][
        "work_unit_id"
    ]
    metrics = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))
    assert metrics["reconciliation"]["ok"] is False
    assert {issue["code"] for issue in metrics["reconciliation"]["issues"]} == {
        "supervising_agent_tokens_missing",
        "work_unit_resource_time_unknown",
    }
    assert {
        case["accounting"]["inclusive"]["gross"]["total_tokens"]
        for case in metrics["cases"]
    } == {12}


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
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    reused = next(row for row in rows if row["event_type"] == "work.reused")
    assert reused["attributes"]["cost_unknown"] is True
    assert reused["attributes"]["dependency_ids"] == []

    case = json.loads((tmp_path / "case_metrics.json").read_text(encoding="utf-8"))[
        "cases"
    ][0]
    assert case["accounting"]["inclusive"]["gross"]["total_tokens"] is None
    assert case["accounting"]["inclusive"]["gross"]["active_seconds"] is None
    assert case["reconciliation"]["ok"] is False
    assert any(
        issue["code"] == "work_unit_cost_unknown"
        for issue in case["reconciliation"]["issues"]
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
        for line in (tmp_path / "lifecycle_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
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
                "evidence_atom_ids": [
                    "target/20260719T083852Z/codex/1:confusion_point:1"
                ],
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
