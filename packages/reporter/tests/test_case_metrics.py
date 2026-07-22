from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reporter import (
    aggregate_case_metrics,
    aggregate_cohort_metrics,
    compare_cohorts,
    load_lifecycle_events,
)

FP_AFTER = {
    "code_commit": "d896a3f3",
    "model": "gpt-5.6-sol",
    "provider": "codex",
    "prompt_hash": "prompt-v4",
    "config_hash": "config-v4",
    "policy_hash": "policy-v4",
    "score_version": "automation_score_v1",
}


def _event(
    event_type: str,
    lifecycle_id: str | None = None,
    *,
    case_id: str | None = None,
    at: str | None = None,
    beneficiaries: list[str] | None = None,
    fingerprint: dict[str, str] | None = FP_AFTER,
    **attributes: Any,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if lifecycle_id is not None:
        context["case_lifecycle_id"] = lifecycle_id
    if case_id is not None:
        context["case_id"] = case_id
    if beneficiaries is not None:
        context["beneficiary_case_lifecycle_ids"] = beneficiaries
    if fingerprint is not None:
        context["system_fingerprint"] = fingerprint
    event: dict[str, Any] = {"type": event_type, "context": context, "attributes": attributes}
    if at is not None:
        event["ts"] = at
    return event


def _stage_completed(lifecycle_id: str, stage: int, at: str) -> dict[str, Any]:
    return _event("stage.completed", lifecycle_id, at=at, stage=stage)


def _acceptance_events() -> list[dict[str, Any]]:
    cases = ["life:addressed", "life:pr"]
    events: list[dict[str, Any]] = [
        _event(
            "lifecycle.opened",
            "life:addressed",
            case_id="case:stable-addressed",
            at="2026-07-21T00:00:00Z",
            origin_ids=["atom:a"],
            atom_created_at="2026-07-20T23:00:00Z",
            admitted_at="2026-07-21T00:00:00Z",
        ),
        _event(
            "lifecycle.opened",
            "life:pr",
            case_id="case:stable-pr",
            at="2026-07-21T00:00:00Z",
            origin_ids=["atom:b"],
            atom_created_at="2026-07-20T22:00:00Z",
            admitted_at="2026-07-21T00:00:00Z",
        ),
        _event(
            "work.completed",
            beneficiaries=cases,
            work_unit_id="shared:stage1",
            shared_work_id="pool:stage12",
            accounting_scope="shared",
            stage=1,
            token_scope="qualification",
            token_usage={"total_tokens": 100},
            active_seconds=10,
            started_at="2026-07-21T00:00:00Z",
            ended_at="2026-07-21T00:00:10Z",
        ),
        _event(
            "work.completed",
            beneficiaries=cases,
            work_unit_id="shared:stage2",
            shared_work_id="pool:stage12",
            dependency_ids=["shared:stage1"],
            accounting_scope="shared",
            stage=2,
            token_scope="qualification",
            token_usage={"total_tokens": 50},
            active_seconds=5,
            machine_wait_seconds=2,
            external_wait_seconds=1,
            queue_wait_seconds=2,
            provider_wait_seconds=1,
            started_at="2026-07-21T00:00:10Z",
            ended_at="2026-07-21T00:00:15Z",
        ),
        _stage_completed("life:addressed", 1, "2026-07-21T00:00:10Z"),
        _stage_completed("life:addressed", 2, "2026-07-21T00:00:15Z"),
        _stage_completed("life:pr", 1, "2026-07-21T00:00:10Z"),
        _stage_completed("life:pr", 2, "2026-07-21T00:00:15Z"),
        _event(
            "model.invocation.completed",
            "life:addressed",
            work_unit_id="addressed:stage3",
            dependency_ids=["shared:stage2"],
            stage=3,
            token_scope="qualification",
            token_usage={"total_tokens": 30},
            active_seconds=3,
            started_at="2026-07-21T00:00:15Z",
            ended_at="2026-07-21T00:00:18Z",
        ),
        _stage_completed("life:addressed", 3, "2026-07-21T00:00:18Z"),
        _event(
            "error.occurred",
            "life:addressed",
            at="2026-07-21T00:00:16Z",
            error_cluster_id="error:self-heal",
        ),
        _event(
            "error.resolved",
            "life:addressed",
            at="2026-07-21T00:00:17Z",
            error_cluster_id="error:self-heal",
            resolution_category="self_healed_same_author",
        ),
        _event(
            "disposition.verified",
            "life:addressed",
            at="2026-07-21T00:00:20Z",
            disposition="already_addressed",
        ),
        _event(
            "model.invocation.completed",
            "life:pr",
            work_unit_id="pr:stages3-6",
            dependency_ids=["shared:stage2"],
            stage=3,
            token_scope="implementation",
            token_usage={"total_tokens": 70},
            active_seconds=7,
            machine_wait_seconds=1,
            external_wait_seconds=4,
            ci_wait_seconds=2,
            approval_wait_seconds=1,
            categorized_external_wait_seconds=1,
            started_at="2026-07-21T00:00:15Z",
            ended_at="2026-07-21T00:00:22Z",
        ),
        _stage_completed("life:pr", 3, "2026-07-21T00:00:22Z"),
        _stage_completed("life:pr", 4, "2026-07-21T00:00:23Z"),
        _stage_completed("life:pr", 5, "2026-07-21T00:00:24Z"),
        _stage_completed("life:pr", 6, "2026-07-21T00:00:25Z"),
        _event(
            "error.occurred",
            "life:pr",
            at="2026-07-21T00:00:21Z",
            error_cluster_id="error:supervisor",
        ),
        _event(
            "intervention.completed",
            "life:pr",
            work_unit_id="support:supervisor",
            intervention_id="intervention:1",
            actor="supervising_agent",
            milestone_id="stage3",
            error_cluster_id="error:supervisor",
            avoidable=True,
            required_for_progress=True,
            accounting_scope="support",
            work_scope="supervising_agent",
            token_usage={"total_tokens": 20},
            active_seconds=2,
            started_at="2026-07-21T00:00:22Z",
            ended_at="2026-07-21T00:00:24Z",
        ),
        _event(
            "error.occurred",
            "life:pr",
            at="2026-07-21T00:00:26Z",
            error_cluster_id="error:unresolved",
        ),
        _event(
            "delivery.started",
            "life:pr",
            at="2026-07-21T00:00:27Z",
            pr_created_at="2026-07-21T00:00:27Z",
        ),
        _event(
            "action.completed",
            "life:pr",
            action_id="manual:pr-create",
            actor="human",
            manual=True,
            policy_mandated=True,
            required_for_progress=True,
            milestone_id="delivery.completed",
            accounting_scope="support",
            work_scope="outside_platform",
            action_family="platform_write",
            operation="create_pull_request",
            token_usage={"total_tokens": 5},
            active_seconds=3,
            started_at="2026-07-21T00:00:27Z",
            ended_at="2026-07-21T00:00:30Z",
        ),
        _event(
            "disposition.verified",
            "life:pr",
            at="2026-07-21T00:00:30Z",
            disposition="pull_request",
        ),
        _event(
            "outcome.verified",
            "life:pr",
            at="2026-07-21T00:00:40Z",
        ),
    ]
    return events


def test_case_and_cohort_accounting_preserves_shared_work_and_exact_dispositions() -> None:
    report = aggregate_case_metrics(_acceptance_events())

    assert report["reconciliation"]["ok"] is True
    assert report["case_count"] == 2
    by_lifecycle = {case["case_lifecycle_id"]: case for case in report["cases"]}
    addressed = by_lifecycle["life:addressed"]
    pr_case = by_lifecycle["life:pr"]

    assert addressed["case_id"] == "case:stable-addressed"
    assert addressed["disposition"] == "already_addressed"
    assert addressed["accounting"]["direct"]["gross"]["total_tokens"] == 30
    assert addressed["accounting"]["inclusive"]["gross"]["total_tokens"] == 180
    assert addressed["errors"]["self_healed_cluster_count"] == 1
    assert addressed["automation_score_v1"]["gross"]["score"] == 100.0
    assert addressed["automation_score_v1"]["certified"] is True
    assert addressed["timing"]["atom_to_disposition_seconds"] == 3620.0
    assert addressed["timing"]["lineage_to_disposition_seconds"] == 20.0
    assert addressed["timing"]["work_interval_union_seconds"] == 18.0
    assert addressed["timing"]["machine_wait_seconds"] == 2.0
    assert addressed["timing"]["external_wait_seconds"] == 1.0
    assert addressed["timing"]["accounted_resource_seconds"] == 21.0
    assert addressed["timing"]["unclassified_seconds"] == 2.0

    assert pr_case["disposition"] == "pr"
    assert pr_case["accounting"]["direct"]["gross"]["total_tokens"] == 70
    assert pr_case["accounting"]["inclusive"]["gross"]["total_tokens"] == 220
    assert pr_case["accounting"]["all_in"]["gross"]["active_seconds"] == 27.0
    assert pr_case["errors"]["externally_resolved_cluster_count"] == 1
    assert pr_case["errors"]["unresolved_terminal_cluster_count"] == 1
    assert pr_case["interventions"]["count"] == 1
    assert pr_case["interventions"]["active_seconds"] == 2.0
    assert pr_case["manual_actions"]["required_for_progress_count"] == 1
    assert pr_case["manual_actions"]["active_seconds"] == 3.0
    assert pr_case["timing"]["pr_create_to_outcome_seconds"] == 13.0
    assert pr_case["automation_score_v1"]["gross"]["score"] == pytest.approx(77.7778)
    assert pr_case["automation_score_v1"]["avoidable"]["score"] == 87.5
    assert pr_case["automation_score_v1"]["certified"] is True
    supervisor_cluster = next(
        cluster
        for cluster in pr_case["errors"]["clusters"]
        if cluster["error_cluster_id"] == "error:supervisor"
    )
    assert supervisor_cluster["resolution_elapsed_seconds"] == 3.0
    assert supervisor_cluster["linked_intervention_count"] == 1
    assert supervisor_cluster["resolution_tokens"] is None
    assert pr_case["manual_actions"]["items"][0]["operation"] == "create_pull_request"

    cohort = aggregate_cohort_metrics(report, cohort_id="acceptance")
    assert cohort["disposition_counts"] == {"already_addressed": 1, "pr": 1}
    # Inclusive per-case totals are 180 + 220, but the shared 150-token pool is counted once.
    assert cohort["accounting"]["inclusive"]["gross"]["total_tokens"] == 250
    assert cohort["accounting"]["all_in"]["gross"]["active_seconds"] == 30.0
    assert cohort["accounting"]["all_in"]["gross"][
        "wall_clock_interval_union_seconds"
    ] == 27.0
    all_in = cohort["accounting"]["all_in"]
    assert all_in["by_token_scope"]["qualification"]["gross"]["total_tokens"] == 180
    assert all_in["by_token_scope"]["implementation"]["gross"]["total_tokens"] == 70
    assert all_in["by_token_scope"]["supervising_agent"]["gross"]["total_tokens"] == 20
    assert all_in["by_token_scope"]["outside_platform"]["gross"]["total_tokens"] == 5
    assert all_in["by_stage"]["stage1.problem_mining"]["gross"]["total_tokens"] == 100
    assert all_in["gross"]["wait_seconds_by_category"] == {
        "queue": 2.0,
        "provider": 1.0,
        "ci": 2.0,
        "approval": 1.0,
        "external": 1.0,
        "unknown": 1.0,
    }
    assert cohort["manual_work"]["work_unit_count"] == 2
    assert cohort["manual_work"]["active_seconds"] == 5.0
    assert cohort["manual_work"]["active_minutes"] == pytest.approx(5 / 60)
    assert cohort["automation_score_v1"]["touchless_terminal_yield"] == 0.5
    assert cohort["automation_score_v1"]["pipeline_autonomous_rate"] == 0.5
    assert cohort["automation_score_v1"]["human_touch_free_rate"] == 0.5
    assert cohort["errors"] == {
        "cluster_count": 3,
        "occurrence_count": 3,
        "self_healed_cluster_count": 1,
        "externally_resolved_cluster_count": 1,
        "unresolved_terminal_cluster_count": 1,
        "open_cluster_count": 0,
        "tolerated_nonblocking_cluster_count": 0,
    }
    assert cohort["by_disposition"]["already_addressed"]["case_distributions"][
        "inclusive_total_tokens"
    ]["median"] == 180.0
    assert cohort["by_disposition"]["pr"]["case_distributions"][
        "inclusive_total_tokens"
    ]["p90"] == 220.0


def test_action_start_and_completion_share_nested_action_identity() -> None:
    started = _event(
        "action.started",
        "life:paired-action",
        at="2026-07-21T00:00:00Z",
        action_id="action:paired",
        actor="supervising_agent",
        manual=True,
        required_for_progress=True,
        action_family="launch",
        operation="run measured command",
        started_at="2026-07-21T00:00:00Z",
    )
    started["event_id"] = "event:action-started"
    completed = _event(
        "action.completed",
        "life:paired-action",
        at="2026-07-21T00:00:03Z",
        action_id="action:paired",
        actor="supervising_agent",
        manual=True,
        required_for_progress=True,
        action_family="launch",
        operation="run measured command",
        active_seconds=3,
        started_at="2026-07-21T00:00:00Z",
        ended_at="2026-07-21T00:00:03Z",
    )
    completed["event_id"] = "event:action-completed"

    report = aggregate_case_metrics([started, completed])
    [case] = report["cases"]

    assert case["manual_actions"]["count"] == 1
    assert case["manual_actions"]["active_seconds"] == 3.0
    assert case["manual_actions"]["items"][0]["id"] == "action:paired"


def test_manual_action_interval_does_not_imply_active_time() -> None:
    started = _event(
        "action.started",
        "life:unknown-action-time",
        at="2026-07-21T00:00:00Z",
        action_id="action:unknown-time",
        actor="supervising_agent",
        manual=True,
        work_unit_id="work:unknown-action-time",
        started_at="2026-07-21T00:00:00Z",
    )
    completed = _event(
        "action.completed",
        "life:unknown-action-time",
        at="2026-07-21T00:00:03Z",
        action_id="action:unknown-time",
        actor="supervising_agent",
        manual=True,
        work_unit_id="work:unknown-action-time",
        started_at="2026-07-21T00:00:00Z",
        ended_at="2026-07-21T00:00:03Z",
        resource_time_unknown=True,
        resource_time_unknown_reason="manual_boundary_child_time_unattributable",
    )

    [case] = aggregate_case_metrics([started, completed])["cases"]

    assert case["manual_actions"]["count"] == 1
    assert case["manual_actions"]["active_seconds"] is None
    assert case["manual_actions"]["known_active_seconds"] == 0
    assert case["manual_actions"]["missing_active_seconds_count"] == 1
    gross = case["accounting"]["all_in"]["gross"]
    assert gross["active_seconds"] is None
    assert gross["known_active_seconds"] == 0
    assert gross["unknown_resource_time_work_units"] == 1
    assert gross["wall_clock_interval_union_seconds"] == 3
    assert any(
        issue["code"] == "work_unit_resource_time_unknown"
        for issue in case["reconciliation"]["issues"]
    )


def test_legacy_telemetry_exec_errors_reuse_paired_action_scopes() -> None:
    group_id = "exec-attempt-group:legacy-scope"
    events = [
        _event(
            "action.completed",
            "life:legacy-telemetry-exec",
            at="2026-07-21T00:00:01Z",
            work_unit_id="work:failed-attempt",
            action_id="action:failed-attempt",
            actor="supervising_agent",
            work_scope="qualification",
            active_seconds=1,
            started_at="2026-07-21T00:00:00Z",
            ended_at="2026-07-21T00:00:01Z",
            telemetry_exec_attempt_group_id=group_id,
        ),
        _event(
            "error.occurred",
            "life:legacy-telemetry-exec",
            at="2026-07-21T00:00:01Z",
            work_unit_id="work:failed-attempt",
            actor="supervising_agent",
            error_cluster_id="error:legacy-scope",
            telemetry_exec_attempt_group_id=group_id,
        ),
        _event(
            "action.completed",
            "life:legacy-telemetry-exec",
            at="2026-07-21T00:00:02Z",
            work_unit_id="work:successful-attempt",
            action_id="action:successful-attempt",
            actor="supervising_agent",
            work_scope="qualification",
            active_seconds=1,
            started_at="2026-07-21T00:00:01Z",
            ended_at="2026-07-21T00:00:02Z",
            telemetry_exec_attempt_group_id=group_id,
        ),
        _event(
            "error.resolved",
            "life:legacy-telemetry-exec",
            at="2026-07-21T00:00:02Z",
            work_unit_id="work:successful-attempt",
            actor="supervising_agent",
            error_cluster_id="error:legacy-scope",
            resolution_mode="resolved_supervisor",
            resolution_work_unit_ids=["work:successful-attempt"],
            resolution_cost_attribution_complete=True,
            telemetry_exec_attempt_group_id=group_id,
        ),
    ]

    report = aggregate_case_metrics(events)

    issue_codes = {item["code"] for item in report["reconciliation"]["issues"]}
    assert "work_unit_scope_conflict" not in issue_codes
    assert "work_unit_token_scope_conflict" not in issue_codes
    units = {unit["work_unit_id"]: unit for unit in report["work_units"]}
    for work_unit_id in ("work:failed-attempt", "work:successful-attempt"):
        assert units[work_unit_id]["scope"] == "support"
        assert units[work_unit_id]["token_scope"] == "qualification"


def test_legacy_manual_exec_wall_time_is_migrated_to_unknown() -> None:
    common = {
        "action_id": "action:legacy-exec",
        "actor": "supervising_agent",
        "manual": True,
        "work_unit_id": "work:legacy-exec",
        "redacted_command": "python tools/continuous_implement_loop.py",
        "command_fingerprint": "legacy-command-fingerprint",
        "started_at": "2026-07-21T00:00:00Z",
    }
    started = _event(
        "action.started",
        "life:legacy-exec",
        at="2026-07-21T00:00:00Z",
        **common,
    )
    completed = _event(
        "action.completed",
        "life:legacy-exec",
        at="2026-07-21T00:07:26Z",
        ended_at="2026-07-21T00:07:26Z",
        active_seconds=446,
        **common,
    )

    [case] = aggregate_case_metrics([started, completed])["cases"]

    assert case["manual_actions"]["count"] == 1
    assert case["manual_actions"]["active_seconds"] is None
    assert case["manual_actions"]["known_active_seconds"] == 0
    gross = case["accounting"]["all_in"]["gross"]
    assert gross["active_seconds"] is None
    assert gross["known_active_seconds"] == 0
    assert gross["wall_clock_interval_union_seconds"] == 446
    [work] = case["accounting"]["all_in"]["work_unit_ids"]
    assert work == "work:legacy-exec"


def test_jsonl_loader_and_missing_reconciliation_withhold_certification(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.jsonl"
    events = [
        _event(
            "lifecycle.opened",
            "life:broken",
            case_id="case:broken",
            at="2026-07-21T01:00:00Z",
            fingerprint=None,
        ),
        _event(
            "work.completed",
            "life:broken",
            work_unit_id="broken:work",
            dependency_ids=["missing:stage1"],
            token_usage={"total_tokens": 10},
            active_seconds=1,
        ),
        _event(
            "disposition.reached",
            "life:broken",
            at="2026-07-21T01:01:00Z",
            disposition="already_addressed",
            fingerprint=None,
        ),
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    loaded = load_lifecycle_events(path)
    assert len(loaded) == 3
    case = aggregate_case_metrics(path)["cases"][0]

    assert case["disposition"] == "already_addressed"
    assert case["disposition_verified"] is False
    assert case["reconciliation"]["ok"] is False
    assert case["automation_score_v1"]["certified"] is False
    assert set(case["automation_score_v1"]["withheld_reasons"]) >= {
        "origin_telemetry_unknown",
        "required_milestones_missing",
        "disposition_not_verified",
        "accounting_reconciliation_failed",
    }


def test_closed_lifecycle_without_disposition_remains_unknown() -> None:
    case = aggregate_case_metrics(
        [
            _event(
                "lifecycle.opened",
                "life:legacy-unknown",
                at="2026-07-21T01:00:00Z",
                fingerprint=None,
            ),
            _event(
                "lifecycle.closed",
                "life:legacy-unknown",
                at="2026-07-21T01:01:00Z",
                fingerprint=None,
            ),
        ]
    )["cases"][0]

    assert case["disposition"] is None
    assert case["lifecycle_status"] == "closed_unclassified"
    assert case["automation_score_v1"]["status"] == "withheld"
    assert case["automation_score_v1"]["gross"]["score"] is None


def test_incomplete_manual_action_telemetry_withholds_zero_counts_and_rates() -> None:
    report = aggregate_case_metrics(
        [
            _event(
                "lifecycle.opened",
                "life:historical-manual-unknown",
                at="2026-07-21T01:00:00Z",
                fingerprint=None,
            ),
            _event(
                "disposition.verified",
                "life:historical-manual-unknown",
                at="2026-07-21T01:01:00Z",
                disposition="already_addressed",
                fingerprint=None,
            ),
            _event(
                "lifecycle.closed",
                "life:historical-manual-unknown",
                at="2026-07-21T01:01:00Z",
                disposition="already_addressed",
                manual_action_telemetry_complete=False,
                work_unit_id="historical:base-work",
                cost_unknown=True,
                fingerprint=None,
            ),
        ]
    )

    case = report["cases"][0]
    assert case["manual_actions"]["count"] is None
    assert case["manual_actions"]["known_count"] == 0
    assert case["manual_actions"]["telemetry_complete"] is False
    assert case["timing"]["manual_active_seconds"] is None
    assert case["accounting"]["all_in"]["gross"]["total_tokens"] is None

    cohort = aggregate_cohort_metrics(report, cohort_id="manual-unknown")
    assert cohort["manual_actions"]["count"] is None
    assert cohort["manual_actions"]["known_count"] == 0
    automation = cohort["automation_score_v1"]
    assert automation["rate_eligible_terminal_case_count"] == 0
    assert automation["touchless_terminal_yield"] is None
    assert "system_fingerprint_missing" in case["automation_score_v1"][
        "withheld_reasons"
    ]


def test_unmaterialized_model_usage_propagates_unknown_instead_of_zero() -> None:
    case = aggregate_case_metrics(
        [
            _event(
                "model.invocation.completed",
                "life:unknown-usage",
                work_unit_id="model:known",
                token_scope="qualification",
                token_usage={"total_tokens": 10},
            ),
            _event(
                "model.invocation.completed",
                "life:unknown-usage",
                work_unit_id="model:unmaterialized",
                token_scope="qualification",
                usage_receipt_path="receipts/unmaterialized.json",
            ),
        ]
    )["cases"][0]

    direct = case["accounting"]["direct"]
    assert direct["gross"]["total_tokens"] is None
    assert direct["gross"]["tokens"]["total_tokens"] is None
    assert direct["gross"]["known_token_subtotal"]["total_tokens"] == 10
    assert direct["completeness"]["total_tokens_complete"] is False
    assert direct["completeness"]["token_ratio"] == 0.5
    assert any(
        issue["code"] == "token_receipt_unmaterialized"
        for issue in case["reconciliation"]["issues"]
    )


def test_reused_prior_work_without_cost_lineage_propagates_unknown_totals() -> None:
    case = aggregate_case_metrics(
        [
            _event(
                "work.completed",
                "life:reused-cost",
                work_unit_id="current:measured",
                token_usage={"total_tokens": 10},
                active_seconds=2,
                started_at="2026-07-21T01:00:00Z",
                ended_at="2026-07-21T01:00:02Z",
            ),
            _event(
                "work.reused",
                "life:reused-cost",
                work_unit_id="prior:unmeasured",
                cost_unknown=True,
                cost_unknown_reason="reused_prior_work_missing_complete_dependency_lineage",
            ),
        ]
    )["cases"][0]

    inclusive = case["accounting"]["inclusive"]
    assert case["accounting"]["direct"]["gross"]["total_tokens"] == 10
    assert inclusive["gross"]["total_tokens"] is None
    assert inclusive["gross"]["known_token_subtotal"]["total_tokens"] == 10
    assert inclusive["gross"]["active_seconds"] is None
    assert inclusive["gross"]["known_active_seconds"] == 2
    assert inclusive["gross"]["wall_clock_interval_union_seconds"] is None
    assert inclusive["gross"]["unknown_cost_work_units"] == 1
    assert inclusive["completeness"]["cost_complete"] is False
    assert case["reconciliation"]["ok"] is False
    assert any(
        issue["code"] == "work_unit_cost_unknown"
        and issue["work_unit_id"] == "prior:unmeasured"
        for issue in case["reconciliation"]["issues"]
    )


def test_missing_accounting_evidence_withholds_automation_score_instead_of_zero() -> None:
    lifecycle_id = "life:unknown-score"
    events = [
        _event(
            "lifecycle.opened",
            lifecycle_id,
            at="2026-07-21T01:00:00Z",
            origin_ids=["atom:unknown-score"],
        ),
        _stage_completed(lifecycle_id, 1, "2026-07-21T01:00:01Z"),
        _stage_completed(lifecycle_id, 2, "2026-07-21T01:00:02Z"),
        _stage_completed(lifecycle_id, 3, "2026-07-21T01:00:03Z"),
        _event(
            "model.invocation.completed",
            lifecycle_id,
            work_unit_id="model:missing-usage",
        ),
        _event(
            "disposition.verified",
            lifecycle_id,
            at="2026-07-21T01:00:04Z",
            disposition="already_addressed",
        ),
    ]

    case = aggregate_case_metrics(events)["cases"][0]

    assert case["automation_score_v1"]["gross"]["score"] is None
    assert case["automation_score_v1"]["avoidable"]["score"] is None
    assert case["automation_score_v1"]["status"] == "withheld"


def test_historical_unknown_error_resolution_remains_open_at_case_closure() -> None:
    lifecycle_id = "life:historical-open"
    case = aggregate_case_metrics(
        [
            _event(
                "error.occurred",
                lifecycle_id,
                at="2026-07-21T01:00:00Z",
                error_cluster_id="error:unknown-resolution",
                resolution_evidence_unknown=True,
            ),
            _event(
                "lifecycle.closed",
                lifecycle_id,
                at="2026-07-21T01:00:05Z",
                attested_self_healed_cluster_ids=["repair:attested-only"],
            ),
        ]
    )["cases"][0]

    assert case["errors"]["cluster_count"] == 1
    assert case["errors"]["open_cluster_count"] == 1
    assert case["errors"]["unresolved_terminal_cluster_count"] == 0
    assert case["errors"]["self_healed_cluster_count"] == 1


def test_error_resolution_cost_is_only_published_with_complete_attribution() -> None:
    case = aggregate_case_metrics(
        [
            _event(
                "lifecycle.opened",
                "life:resolution-cost",
                at="2026-07-21T01:00:00Z",
                origin_ids=["atom:resolution-cost"],
            ),
            _event(
                "error.occurred",
                "life:resolution-cost",
                at="2026-07-21T01:00:01Z",
                error_cluster_id="error:repair",
            ),
            _event(
                "model.invocation.completed",
                "life:resolution-cost",
                work_unit_id="repair:model",
                token_scope="supervising_agent",
                token_usage={"total_tokens": 12},
                started_at="2026-07-21T01:00:01Z",
                ended_at="2026-07-21T01:00:03Z",
                active_seconds=2,
            ),
            _event(
                "error.resolved",
                "life:resolution-cost",
                at="2026-07-21T01:00:04Z",
                error_cluster_id="error:repair",
                resolution_mode="self_healed_controller",
                resolution_work_unit_ids=["repair:model"],
                resolution_cost_attribution_complete=True,
            ),
            _event(
                "disposition.verified",
                "life:resolution-cost",
                at="2026-07-21T01:00:05Z",
                disposition="failed_incomplete",
            ),
        ]
    )["cases"][0]

    cluster = case["errors"]["clusters"][0]
    assert cluster["resolution_category"] == "self_healed_controller"
    assert cluster["resolution_elapsed_seconds"] == 3.0
    assert cluster["resolution_total_tokens"] == 12
    assert cluster["resolution_tokens"]["input_tokens"] is None
    assert cluster["resolution_cost_attribution_complete"] is True


@pytest.mark.parametrize(
    ("disposition", "stages", "expected_score"),
    [
        ("already_addressed", [1, 2, 3], 100.0),
        ("non_actionable", [1, 2, 3], 100.0),
        ("duplicate", [1], 100.0),
        ("superseded", [1], 100.0),
        ("pr", [1, 2, 3, 4, 5, 6], 100.0),
        ("failed_incomplete", [], 0.0),
    ],
)
def test_all_exact_disposition_paths(
    disposition: str, stages: list[int], expected_score: float
) -> None:
    lifecycle_id = f"life:{disposition}"
    events = [
        _event(
            "lifecycle.opened",
            lifecycle_id,
            case_id=f"case:{disposition}",
            at="2026-07-21T02:00:00Z",
            origin_ids=[f"atom:{disposition}"],
        )
    ]
    events.extend(
        _stage_completed(lifecycle_id, stage, f"2026-07-21T02:00:{stage:02d}Z")
        for stage in stages
    )
    if disposition == "pr":
        events.append(
            _event(
                "delivery.completed",
                lifecycle_id,
                at="2026-07-21T02:00:07Z",
            )
        )
    events.append(
        _event(
            "disposition.verified",
            lifecycle_id,
            at="2026-07-21T02:00:08Z",
            disposition=disposition,
        )
    )

    case = aggregate_case_metrics(events)["cases"][0]
    assert case["disposition"] == disposition
    assert case["automation_score_v1"]["gross"]["score"] == expected_score
    if disposition == "failed_incomplete":
        assert case["automation_score_v1"]["status"] == "failed_or_invalid"
    else:
        assert case["automation_score_v1"]["certified"] is True


def test_active_case_is_not_coerced_to_failed_disposition() -> None:
    report = aggregate_case_metrics(
        [
            _event(
                "lifecycle.opened",
                "life:active",
                case_id="case:active",
                at="2026-07-21T03:00:00Z",
                origin_ids=["atom:active"],
            )
        ]
    )
    case = report["cases"][0]
    assert case["disposition"] is None
    assert case["lifecycle_status"] == "active"
    assert case["automation_score_v1"]["status"] == "pending"

    cohort = aggregate_cohort_metrics(report)
    assert cohort["disposition_counts"] == {}
    assert cohort["active_case_count"] == 1


@pytest.mark.parametrize(
    "resolution_mode",
    [
        "self_healed_same_author",
        "self_healed_controller",
        "resolved_supervisor",
        "resolved_human",
        "resolved_external",
        "tolerated_nonblocking",
        "unresolved_terminal",
    ],
)
def test_exact_terminal_error_resolution_modes(resolution_mode: str) -> None:
    lifecycle_id = f"life:{resolution_mode}"
    case = aggregate_case_metrics(
        [
            _event(
                "lifecycle.opened",
                lifecycle_id,
                case_id=f"case:{resolution_mode}",
                at="2026-07-21T04:00:00Z",
                origin_ids=["atom:1"],
            ),
            _event(
                "error.occurred",
                lifecycle_id,
                at="2026-07-21T04:00:01Z",
                error_cluster_id="error:1",
            ),
            _event(
                "error.resolved",
                lifecycle_id,
                at="2026-07-21T04:00:02Z",
                error_cluster_id="error:1",
                resolution_mode=resolution_mode,
            ),
            _event(
                "disposition.verified",
                lifecycle_id,
                at="2026-07-21T04:00:03Z",
                disposition="failed_incomplete",
            ),
        ]
    )["cases"][0]
    assert case["errors"]["by_resolution"] == {resolution_mode: 1}


def test_open_error_mode_remains_distinct_for_active_lifecycle() -> None:
    case = aggregate_case_metrics(
        [
            _event(
                "lifecycle.opened",
                "life:open-error",
                case_id="case:open-error",
                at="2026-07-21T05:00:00Z",
                origin_ids=["atom:1"],
            ),
            _event(
                "error.occurred",
                "life:open-error",
                at="2026-07-21T05:00:01Z",
                error_cluster_id="error:open",
            ),
        ]
    )["cases"][0]
    assert case["errors"]["by_resolution"] == {"open": 1}
    assert case["errors"]["open_cluster_count"] == 1


def test_legacy_reused_action_work_unit_is_split_without_losing_cost() -> None:
    legacy_work_id = "work:legacy-action-bundle"
    events = [
        _event(
            "lifecycle.opened",
            "life:legacy-actions",
            case_id="case:legacy-actions",
            at="2026-07-21T00:00:00Z",
            origin_ids=["atom:legacy-actions"],
        ),
        _event(
            "action.started",
            "life:legacy-actions",
            at="2026-07-21T00:00:01Z",
            work_unit_id=legacy_work_id,
            action_id="action:one",
            actor="supervisor",
            manual=True,
            stage="repair",
            started_at="2026-07-21T00:00:01Z",
        ),
        _event(
            "action.completed",
            "life:legacy-actions",
            at="2026-07-21T00:00:02Z",
            work_unit_id=legacy_work_id,
            action_id="action:one",
            actor="supervisor",
            manual=True,
            stage="repair",
            active_seconds=1,
            started_at="2026-07-21T00:00:01Z",
            ended_at="2026-07-21T00:00:02Z",
        ),
        _event(
            "action.started",
            "life:legacy-actions",
            at="2026-07-21T00:00:03Z",
            work_unit_id=legacy_work_id,
            action_id="action:two",
            actor="supervisor",
            manual=True,
            stage="delivery",
            started_at="2026-07-21T00:00:03Z",
        ),
        _event(
            "action.completed",
            "life:legacy-actions",
            at="2026-07-21T00:00:05Z",
            work_unit_id=legacy_work_id,
            action_id="action:two",
            actor="supervisor",
            manual=True,
            stage="delivery",
            active_seconds=2,
            started_at="2026-07-21T00:00:03Z",
            ended_at="2026-07-21T00:00:05Z",
        ),
        _event(
            "work.completed",
            "life:legacy-actions",
            work_unit_id="work:dependent",
            dependency_ids=[legacy_work_id],
            active_seconds=3,
            started_at="2026-07-21T00:00:05Z",
            ended_at="2026-07-21T00:00:08Z",
        ),
    ]

    report = aggregate_case_metrics(events)

    assert report["reconciliation"]["ok"] is True
    [migration] = report["normalization"]["legacy_action_work_unit_splits"]
    assert migration["source_work_unit_id"] == legacy_work_id
    assert migration["source_event_count"] == 4
    concrete_ids = {
        binding["work_unit_id"] for binding in migration["concrete_bindings"]
    }
    assert len(concrete_ids) == 2
    units = {unit["work_unit_id"]: unit for unit in report["work_units"]}
    assert set(units) == concrete_ids | {"work:dependent"}
    assert set(units["work:dependent"]["dependency_ids"]) == concrete_ids
    case = report["cases"][0]
    assert case["manual_actions"]["count"] == 2
    assert case["accounting"]["all_in"]["gross"]["active_seconds"] == 6.0
    cohort = aggregate_cohort_metrics(report)
    assert cohort["normalization"]["legacy_action_work_unit_splits"] == [migration]


def test_legacy_action_split_does_not_hide_non_action_identity_conflict() -> None:
    legacy_work_id = "work:genuinely-ambiguous"
    events = [
        _event(
            "action.completed",
            "life:ambiguous",
            work_unit_id=legacy_work_id,
            action_id="action:one",
            actor="supervisor",
            manual=True,
            stage="repair",
            active_seconds=1,
        ),
        _event(
            "action.completed",
            "life:ambiguous",
            work_unit_id=legacy_work_id,
            action_id="action:two",
            actor="supervisor",
            manual=True,
            stage="delivery",
            active_seconds=2,
        ),
        _event(
            "work.completed",
            "life:ambiguous",
            work_unit_id=legacy_work_id,
            stage="qualification",
            active_seconds=3,
        ),
    ]

    report = aggregate_case_metrics(events)

    assert report["normalization"]["legacy_action_work_unit_splits"] == []
    assert report["reconciliation"]["ok"] is False
    assert any(
        issue["code"] == "work_unit_stage_conflict"
        for issue in report["reconciliation"]["issues"]
    )


def test_cohort_publishes_mixed_version_lifecycle_warning() -> None:
    events = [
        _event(
            "lifecycle.opened",
            "life:one",
            case_id="case:one",
            at="2026-07-21T00:00:00Z",
            fingerprint={**FP_AFTER, "code_commit": "one"},
        ),
        _event(
            "lifecycle.opened",
            "life:two",
            case_id="case:two",
            at="2026-07-21T00:00:00Z",
            fingerprint={**FP_AFTER, "code_commit": "two"},
        ),
    ]

    cohort = aggregate_cohort_metrics(events)

    assert cohort["version_boundaries"]["mixed_system_fingerprints"] is True
    assert cohort["version_boundaries"]["system_fingerprint_count"] == 2
    assert [warning["code"] for warning in cohort["version_warnings"]] == [
        "mixed_system_fingerprints"
    ]

    missing = aggregate_cohort_metrics(
        [
            _event(
                "lifecycle.opened",
                "life:missing",
                case_id="case:missing",
                at="2026-07-21T00:00:00Z",
                fingerprint={},
            )
        ]
    )
    assert missing["version_boundaries"]["missing_system_fingerprint_count"] == 1
    assert missing["version_warnings"][0]["code"] == "missing_system_fingerprints"


def test_compare_cohorts_reports_mapping_fingerprints_percentages_and_objectives() -> None:
    before_events = _acceptance_events()
    after_events = json.loads(json.dumps(before_events))
    for event in after_events:
        context = event.get("context", {})
        if isinstance(context, dict):
            context["system_fingerprint"] = {
                "controller": "next",
                "model": "gpt-5.6-sol",
                "prompts": "v5",
            }
        attributes = event.get("attributes", {})
        if isinstance(attributes, dict) and event.get("type") == "model.invocation.completed":
            usage = attributes.get("token_usage")
            if isinstance(usage, dict):
                usage["total_tokens"] = int(usage["total_tokens"]) // 2

    comparison = compare_cohorts(
        aggregate_cohort_metrics(before_events, cohort_id="before"),
        aggregate_cohort_metrics(after_events, cohort_id="after"),
    )

    assert comparison["system_fingerprint_comparison"]["complete"] is True
    assert comparison["system_fingerprint_comparison"]["changed"] is True
    assert comparison["interpretation"].startswith("Factual deltas only")
    rows = {row["metric"]: row for row in comparison["factual_metric_rows"]}
    inclusive_tokens = rows["accounting.inclusive.gross.total_tokens"]
    assert inclusive_tokens["before"] == 250.0
    assert inclusive_tokens["after"] == 200.0
    assert inclusive_tokens["absolute_delta"] == -50.0
    assert inclusive_tokens["percentage_delta"] == -20.0
    assert inclusive_tokens["objective"] == "lower_is_better"
    assert inclusive_tokens["objective_alignment"] == "improved"
