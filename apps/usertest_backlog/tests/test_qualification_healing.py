from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from usertest_backlog.workflows.qualification_healing import (
    AuthorRevision,
    _compatible_route_groups,
    build_pending_repaired_shadow_run,
    consume_qualification_corrections,
    correction_feedback_document,
    pending_repaired_shadow_run_errors,
    qualification_correction_consumption_errors,
    qualification_correction_route_errors,
)


def _hash(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _route(
    *,
    status: str = "same_author_resume",
    correctability: str = "correctable",
    categories: list[str] | None = None,
    stage: str = "future_causal_synthesis",
) -> dict[str, Any]:
    provenance = {
        "provenance_source": "runner_stage_role_history",
        "authoring_stage": stage,
        "agent_session_id": "11111111-1111-4111-8111-111111111111",
        "workspace_dir": "C:/retained/workspace",
        "repository_revision": "a" * 40,
        "exact_session_continuation": True,
        "workspace_continuity_verified": True,
        "original_author_cost_seconds": 100.0,
    }
    route: dict[str, Any] = {
        "schema_version": 1,
        "feedback_kind": "accepted_output_quality",
        "authoring_stage": stage,
        "target_identity": "opaque:target",
        "output_kind": "future_output",
        "output_sha256": "a" * 64,
        "quality": "bad",
        "bad_severity": "noncritical",
        "bad_categories": categories or ["root_path_unchanged", "residual_recurrence"],
        "rationale": "The retained output does not address the observed mechanism.",
        "actionable_label_ids": ["held-out:case"],
        "correctability": correctability,
        "route_status": status,
        "agent_session_id": provenance["agent_session_id"],
        "workspace_dir": provenance["workspace_dir"],
        "author_attempt_identity": {"attempt_number": 1, "response_sha256": "b" * 64},
        "author_provenance": provenance,
        "restart_from_stage": stage,
        "rerun_downstream_stages": [stage, "ticket_assembly"],
        "consumption_status": "pending_orchestration",
        "consumption_receipt": None,
    }
    route["route_sha256"] = _hash(route)
    return route


def test_open_route_contract_does_not_allowlist_future_stages_or_defects() -> None:
    route = _route(
        stage="future_causal_synthesis",
        categories=["previously_unforeseen_semantic_failure"],
    )

    assert qualification_correction_route_errors(route) == []


def test_same_author_correction_counts_fewer_different_errors_as_progress() -> None:
    route = _route()
    calls: list[dict[str, Any]] = []
    candidates = iter(
        [
            AuthorRevision(
                payload={"revision": 2},
                validation_errors=("newly_exposed_boundary_error",),
                valid_item_keys=("retained:valid",),
                agent_session_id=route["agent_session_id"],
                workspace_dir=route["workspace_dir"],
                cost_seconds=20.0,
            ),
            AuthorRevision(
                payload={"revision": 3},
                validation_errors=(),
                valid_item_keys=("retained:valid", "repaired:target"),
                agent_session_id=route["agent_session_id"],
                workspace_dir=route["workspace_dir"],
                cost_seconds=25.0,
            ),
        ]
    )

    def invoke(**kwargs: Any) -> AuthorRevision:
        calls.append(kwargs)
        return next(candidates)

    reruns: list[dict[str, Any]] = []

    def rerun(**kwargs: Any) -> dict[str, Any]:
        reruns.append(kwargs)
        return {"artifact_sha256": "c" * 64, "stages": list(kwargs["stages"])}

    result = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"revision": 1},
        invoke_exact_author=invoke,
        rerun_downstream=rerun,
    )

    assert result["accepted_repair_count"] == 1
    assert result["unresolved_route_count"] == 0
    assert result["release_qualification_eligible"] is False
    receipt = result["route_receipts"][0]
    assert receipt["status"] == "corrected"
    assert receipt["assessments"][0]["before_error_count"] == 2
    assert receipt["assessments"][0]["after_error_count"] == 1
    assert receipt["assessments"][0]["global_best_updated"] is True
    assert receipt["assessments"][0]["reason"] == "error_count_decreased"
    assert len(calls) == 2
    assert calls[1]["current_payload"] == {"revision": 2}
    assert len(reruns) == 1
    assert reruns[0]["stages"] == ["future_causal_synthesis", "ticket_assembly"]
    assert qualification_correction_consumption_errors(result) == []


def test_same_author_same_stage_findings_share_one_correction_frontier() -> None:
    first = _route(categories=["first_independent_finding"])
    second = _route(categories=["second_independent_finding"])
    first.pop("route_sha256")
    first["author_provenance"] = {
        **first["author_provenance"],
        "problem_id": "problem:first",
        "case_id": "case:first",
        "miner_tag": "assignment:one",
        "stage1_correction_adapter": "coverage_review",
        "evidence_atom_ids": ["atom:first"],
    }
    first["route_sha256"] = _hash(first)
    second.pop("route_sha256")
    second["target_identity"] = "opaque:second-target"
    second["output_sha256"] = "f" * 64
    second["author_provenance"] = {
        **second["author_provenance"],
        "problem_id": "problem:second",
        "case_id": "case:second",
        "miner_tag": "assignment:one",
        "stage1_correction_adapter": "coverage_review",
        "evidence_atom_ids": ["atom:second"],
    }
    second["route_sha256"] = _hash(second)
    invocations: list[dict[str, Any]] = []
    reruns: list[dict[str, Any]] = []

    def invoke(**kwargs: Any) -> AuthorRevision:
        invocations.append(kwargs)
        feedback = kwargs["feedback"]
        assert feedback["feedback_kind"] == "grouped_independent_findings"
        assert [item["route_sha256"] for item in feedback["findings"]] == [
            first["route_sha256"],
            second["route_sha256"],
        ]
        assert feedback["evidence_atom_ids"] == ["atom:first", "atom:second"]
        return AuthorRevision(
            payload={"both_findings_addressed": True},
            validation_errors=(),
            valid_item_keys=("first", "second"),
            agent_session_id=first["agent_session_id"],
            workspace_dir=first["workspace_dir"],
            cost_seconds=20.0,
        )

    result = consume_qualification_corrections(
        routes=[first, second],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"both_findings_addressed": False},
        invoke_exact_author=invoke,
        rerun_downstream=lambda **kwargs: reruns.append(kwargs) or {"rerun": True},
    )

    assert len(invocations) == 1
    assert len(reruns) == 1
    assert result["accepted_repair_count"] == 2
    assert result["accepted_repair_group_count"] == 1
    assert result["unresolved_route_count"] == 0
    assert [receipt["status"] for receipt in result["route_receipts"]] == [
        "corrected",
        "corrected",
    ]


def test_paused_frontier_resumes_same_payload_session_cost_and_attempt_chain() -> None:
    route = _route(categories=["original_error"])
    route.pop("route_sha256")
    route["author_provenance"] = {
        **route["author_provenance"],
        "original_author_cost_seconds": 1.0,
    }
    route["route_sha256"] = _hash(route)
    first_revisions = iter(
        [
            AuthorRevision(
                payload={"revision": revision},
                validation_errors=("different_error",),
                valid_item_keys=(),
                agent_session_id=route["agent_session_id"],
                workspace_dir=route["workspace_dir"],
                cost_seconds=1.0,
            )
            for revision in (2, 3, 4)
        ]
    )
    first = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"revision": 1},
        invoke_exact_author=lambda **_kwargs: next(first_revisions),
        rerun_downstream=lambda **_kwargs: {},
    )
    frontier = first["route_receipts"][0]["correction_frontier"]
    calls: list[dict[str, Any]] = []

    def invoke(**kwargs: Any) -> AuthorRevision:
        calls.append(kwargs)
        return AuthorRevision(
            payload={"revision": 5},
            validation_errors=(),
            valid_item_keys=("fixed",),
            agent_session_id=route["agent_session_id"],
            workspace_dir=route["workspace_dir"],
            cost_seconds=2.0,
        )

    resumed = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: pytest.fail(
            "resume must not reload the source payload"
        ),
        invoke_exact_author=invoke,
        rerun_downstream=lambda **_kwargs: {},
        resume_frontiers={route["route_sha256"]: frontier},
    )

    assert first["route_receipts"][0]["status"] == (
        "repairable_paused:"
        "consecutive_nonadvancing_corrections_require_adjudication"
    )
    assert resumed["accepted_repair_count"] == 1
    assert len(calls) == 1
    assert calls[0]["attempt_number"] == 5
    assert calls[0]["current_payload"] == {"revision": 4}
    assert calls[0]["prior_assessment"].reason == "new_state_remains_repairable"
    resumed_frontier = resumed["route_receipts"][0]["correction_frontier"]
    assert [item["payload"] for item in resumed_frontier["attempts"]] == [
        {"revision": 1},
        {"revision": 2},
        {"revision": 3},
        {"revision": 4},
        {"revision": 5},
    ]
    assert resumed_frontier["total_correction_cost"] == 5.0


def test_resume_frontier_rejects_workspace_binding_rewrite() -> None:
    route = _route(categories=["original_error"])
    route.pop("route_sha256")
    route["author_provenance"] = {
        **route["author_provenance"],
        "original_author_cost_seconds": 1.0,
    }
    route["route_sha256"] = _hash(route)
    first_revisions = iter(
        [
            AuthorRevision(
                payload={"revision": revision},
                validation_errors=("different_error",),
                valid_item_keys=(),
                agent_session_id=route["agent_session_id"],
                workspace_dir=route["workspace_dir"],
                cost_seconds=1.0,
            )
            for revision in (2, 3, 4)
        ]
    )
    first = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"revision": 1},
        invoke_exact_author=lambda **_kwargs: next(first_revisions),
        rerun_downstream=lambda **_kwargs: {},
    )
    frontier = dict(first["route_receipts"][0]["correction_frontier"])
    frontier["workspace_dir"] = "C:/different/workspace"
    frontier.pop("content_sha256")
    frontier["content_sha256"] = _hash(frontier)

    with pytest.raises(
        ValueError,
        match="qualification_correction_resume_frontier_binding_mismatch",
    ):
        consume_qualification_corrections(
            routes=[route],
            source_pending_run_sha256="d" * 64,
            source_adjudication_sha256="e" * 64,
            load_current_payload=lambda _route: {"revision": 1},
            invoke_exact_author=lambda **_kwargs: pytest.fail("must not invoke"),
            rerun_downstream=lambda **_kwargs: {},
            resume_frontiers={route["route_sha256"]: frontier},
        )


@pytest.mark.parametrize(
    "identity_change",
    [
        "author_attempt_identity",
        "repository_revision",
        "workspace_manifest_sha256",
        "shared_response_identity",
    ],
)
def test_route_groups_never_mix_distinct_exact_author_attempts(
    identity_change: str,
) -> None:
    first = _route(categories=["first"])
    second = _route(categories=["second"])
    second.pop("route_sha256")
    second["target_identity"] = "opaque:second"
    second["output_sha256"] = "f" * 64
    if identity_change == "author_attempt_identity":
        second["author_attempt_identity"] = {
            "attempt_number": 2,
            "response_sha256": "c" * 64,
        }
    else:
        second["author_provenance"] = {
            **second["author_provenance"],
            identity_change: (
                "b" * 40 if identity_change == "repository_revision" else "c" * 64
            ),
        }
    second["route_sha256"] = _hash(second)

    assert len(_compatible_route_groups([first, second])) == 2


def test_grouped_partial_revision_retains_every_route_until_all_targets_are_present() -> None:
    first = _route(categories=["first"])
    second = _route(categories=["second"])
    for route, expected_key in ((first, "item:first"), (second, "item:second")):
        route.pop("route_sha256")
        route["causal_target"] = {
            "problem_ids": [],
            "case_ids": [],
            "evidence_atom_ids": [],
            "actionable_label_ids": ["held-out:case"],
            "expected_item_keys": [expected_key],
        }
    second["target_identity"] = "opaque:second"
    second["output_sha256"] = "f" * 64
    first["route_sha256"] = _hash(first)
    second["route_sha256"] = _hash(second)
    calls = 0
    rerun_called = False

    def invoke(**_kwargs: Any) -> AuthorRevision:
        nonlocal calls
        calls += 1
        return AuthorRevision(
            payload={"item:first": "fixed", "item:second": "omitted"},
            validation_errors=(),
            valid_item_keys=("item:first",),
            agent_session_id=first["agent_session_id"],
            workspace_dir=first["workspace_dir"],
            cost_seconds=100.0,
        )

    def rerun(**_kwargs: Any) -> dict[str, Any]:
        nonlocal rerun_called
        rerun_called = True
        return {}

    result = consume_qualification_corrections(
        routes=[first, second],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {
            "item:first": "old",
            "item:second": "old",
        },
        invoke_exact_author=invoke,
        rerun_downstream=rerun,
    )

    assert calls >= 1
    assert result["accepted_repair_count"] == 0
    assert result["accepted_repair_group_count"] == 0
    assert result["unresolved_route_count"] == 2
    assert rerun_called is False
    assert all(
        receipt["accepted_payload_sha256"] is None
        and receipt["authored_work_disposition"] == "retained"
        and receipt["status"].startswith(("repairable_paused:", "stalled:"))
        for receipt in result["route_receipts"]
    )
    assert any(
        error
        == "qualification_correction_target_omitted:" + second["route_sha256"]
        for attempt in result["route_receipts"][0]["attempts"]
        for error in attempt["validation_errors"]
    )


def test_one_route_requires_every_target_atom_disposition_not_any_intersection() -> None:
    route = _route(categories=["false_rejection"])
    route.pop("route_sha256")
    route["causal_target"] = {
        "problem_ids": [],
        "case_ids": [],
        "evidence_atom_ids": ["atom:one", "atom:two"],
        "actionable_label_ids": ["held-out:case"],
        "expected_item_keys": ["atom:one", "atom:two"],
    }
    route["route_sha256"] = _hash(route)

    result = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"atoms": []},
        invoke_exact_author=lambda **_kwargs: AuthorRevision(
            payload={"atoms": [{"atom_id": "one", "disposition": "supports_case"}]},
            validation_errors=(),
            valid_item_keys=("atom:one",),
            agent_session_id=route["agent_session_id"],
            workspace_dir=route["workspace_dir"],
            cost_seconds=100.0,
        ),
        rerun_downstream=lambda **_kwargs: {},
    )

    assert result["accepted_repair_count"] == 0
    assert any(
        "qualification_correction_target_omitted:" + route["route_sha256"]
        in attempt["validation_errors"]
        for attempt in result["route_receipts"][0]["attempts"]
    )


def test_session_or_workspace_change_never_replaces_retained_frontier() -> None:
    route = _route(categories=["one_finding"])
    revisions = iter(
        [
            AuthorRevision(
                payload={"unsafe": "different-session"},
                validation_errors=(),
                valid_item_keys=(),
                agent_session_id="22222222-2222-4222-8222-222222222222",
                workspace_dir=route["workspace_dir"],
                cost_seconds=1.0,
            )
        ]
    )
    rerun_called = False

    def rerun(**_kwargs: Any) -> dict[str, Any]:
        nonlocal rerun_called
        rerun_called = True
        return {}

    result = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"retained": True},
        invoke_exact_author=lambda **_kwargs: next(revisions),
        rerun_downstream=rerun,
    )

    receipt = result["route_receipts"][0]
    assert receipt["status"] == "repairable_paused:agent_session_changed"
    assert receipt["best_payload_sha256"] == _hash({"retained": True})
    assert receipt["accepted_payload_sha256"] is None
    assert rerun_called is False


def test_uncorrectable_route_is_retained_without_wasteful_invocation() -> None:
    route = _route(status="uncorrectable", correctability="uncorrectable")

    def forbidden(**_kwargs: Any) -> AuthorRevision:
        raise AssertionError("uncorrectable route must not invoke a model")

    result = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"retained": True},
        invoke_exact_author=forbidden,
        rerun_downstream=lambda **_kwargs: {},
    )

    assert result["accepted_repair_count"] == 0
    assert result["route_receipts"][0]["status"] == "uncorrectable"


def test_same_author_feedback_carries_hash_bound_original_source_finding() -> None:
    finding_body = {
        "finding_id": "qualification-source-finding:one",
        "source_adjudication_sha256": "e" * 64,
        "finding_kind": "accepted_output_quality",
        "source_item_sha256": "f" * 64,
        "source_output_ref": {
            "output_kind": "plan",
            "output_sha256": "a" * 64,
        },
        "actionable_label_ids": ["held-out:case"],
        "quality": "bad",
        "bad_severity": "noncritical",
        "bad_categories": ["root_path_unchanged"],
        "rationale": "The original causal mechanism remains unchanged.",
        "correctability": "correctable",
        "causal_target": {
            "problem_ids": ["problem:one"],
            "case_ids": ["case:one"],
            "evidence_atom_ids": ["atom:one"],
            "actionable_label_ids": ["held-out:case"],
            "expected_item_keys": ["plan:one"],
        },
        "route_sha256s": ["1" * 64],
    }
    finding = {**finding_body, "finding_sha256": _hash(finding_body)}
    context_body = {
        "finding_id": finding["finding_id"],
        "finding_sha256": finding["finding_sha256"],
        "original_finding": finding,
        "origin_finding_contexts": [],
        "current_resolution": {
            "finding_id": finding["finding_id"],
            "status": "partially_resolved",
            "rationale": "One recurrence path still remains.",
        },
        "required_outcome": "Address the original finding and retain valid work.",
    }
    context = {**context_body, "content_sha256": _hash(context_body)}
    route = _route(categories=["root_path_unchanged"])
    route.pop("route_sha256")
    route["source_correction_finding_ids"] = [finding["finding_id"]]
    route["source_correction_finding_sha256s"] = [finding["finding_sha256"]]
    route["source_correction_findings"] = [finding]
    route["source_correction_required_contexts"] = [context]
    route["route_sha256"] = _hash(route)

    feedback = correction_feedback_document(
        route,
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
    )

    assert feedback["source_correction_finding_ids"] == [finding["finding_id"]]
    assert feedback["source_correction_findings"] == [finding]
    assert feedback["source_correction_required_contexts"] == [context]
    assert feedback["content_sha256"] == _hash(
        {key: value for key, value in feedback.items() if key != "content_sha256"}
    )


def test_novel_but_nonimproving_corrections_pause_after_bounded_churn() -> None:
    route = _route(categories=["one_finding"])
    calls = 0

    def invoke(**_kwargs: Any) -> AuthorRevision:
        nonlocal calls
        calls += 1
        return AuthorRevision(
            payload={"revision": calls + 1},
            validation_errors=(f"different_unresolved_error_{calls}",),
            valid_item_keys=(),
            agent_session_id=route["agent_session_id"],
            workspace_dir=route["workspace_dir"],
            cost_seconds=60.0,
        )

    result = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"revision": 1},
        invoke_exact_author=invoke,
        rerun_downstream=lambda **_kwargs: {},
    )

    receipt = result["route_receipts"][0]
    assert calls == 3
    assert receipt["status"] == (
        "repairable_paused:"
        "consecutive_nonadvancing_corrections_require_adjudication"
    )
    assert receipt["metrics"]["total_correction_cost_seconds"] == 180.0
    assert receipt["accepted_payload_sha256"] is None


def test_repaired_pending_run_requires_readjudication_and_cannot_qualify(
    tmp_path: Path,
) -> None:
    route = _route(categories=["one_finding"])
    consumption = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"revision": 1},
        invoke_exact_author=lambda **_kwargs: AuthorRevision(
            payload={"revision": 2},
            validation_errors=(),
            valid_item_keys=("fixed",),
            agent_session_id=route["agent_session_id"],
            workspace_dir=route["workspace_dir"],
        ),
        rerun_downstream=lambda **_kwargs: {"artifact_sha256": "f" * 64},
    )
    pending = build_pending_repaired_shadow_run(
        correction_consumption=consumption,
        qualification_manifest_sha256="1" * 64,
        repaired_backlog_sha256="2" * 64,
        repaired_pending_run_sha256="4" * 64,
        repaired_artifact_receipts=[{"name": "stage", "sha256": "3" * 64}],
        correction_consumption_path=str(tmp_path / "consumption.json"),
    )

    assert pending_repaired_shadow_run_errors(pending) == []
    assert pending["same_corpus_feedback_exposed"] is True
    assert pending["release_qualification_eligible"] is False
    assert pending["fresh_independent_readjudication_required"] is True
    tampered = dict(pending)
    tampered["release_qualification_eligible"] = True
    assert "pending_repaired_shadow_run_sha256_mismatch" in pending_repaired_shadow_run_errors(
        tampered
    )
    assert "pending_repaired_shadow_run_must_not_qualify_release" in (
        pending_repaired_shadow_run_errors(tampered)
    )
