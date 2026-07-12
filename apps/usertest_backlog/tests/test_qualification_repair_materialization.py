from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from backlog_core import BacklogPolicyConfig

from usertest_backlog.workflows import qualification_repair_materialization as materialization
from usertest_backlog.workflows.qualification_healing import (
    AuthorRevision,
    consume_qualification_corrections,
    pending_repaired_shadow_run_errors,
)
from usertest_backlog.workflows.qualification_repair_runtime import (
    QualificationRepairRuntimeResult,
)


def _hash(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _qualified_fallback_candidate(
    root: Path,
    *,
    pending_run_sha256: str,
    error_count: int,
) -> tuple[Path, Path, Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    backlog_path = root / "pending.backlog.json"
    adjudication_path = root / "output_adjudication.json"
    report_path = root / "raw_report.json"
    bundle_path = root / "phase1.bundle.json"
    _write(backlog_path, {"tickets": [{"id": root.name}]})
    _write(adjudication_path, {"pending_run_sha256": pending_run_sha256})
    report = {
        "passed": True,
        "qualification_basis_sha256": "a" * 64,
        "qualification": {
            "status": "verified",
            "useful_output_verified": True,
            "stability_sha256": "b" * 64,
            "counts": {
                "accepted_bad": error_count,
                "accepted_unknown": 0,
                "false_rejected_good": 0,
                "undispositioned_actionable_cases": 0,
            },
        },
    }
    wrapper_body = {
        "contract_kind": "qualification_raw_first_pass_report",
        "pending_run_sha256": pending_run_sha256,
        "report": report,
    }
    _write(report_path, {**wrapper_body, "content_sha256": _hash(wrapper_body)})
    bundle_body = {
        "contract_kind": "qualification_phase1_bundle",
        "source_pending_run_sha256": pending_run_sha256,
        "backlog": {
            "snapshot_path": str(backlog_path.resolve()),
            "sha256": sha256(backlog_path.read_bytes()).hexdigest(),
        },
        "qualification_output_adjudication": {
            "snapshot_path": str(adjudication_path.resolve()),
            "sha256": sha256(adjudication_path.read_bytes()).hexdigest(),
        },
    }
    _write(bundle_path, {**bundle_body, "content_sha256": _hash(bundle_body)})
    return backlog_path, report_path, adjudication_path, bundle_path


def test_best_qualified_fallback_persists_across_regression_and_only_strictly_improves(
    tmp_path: Path,
) -> None:
    first_paths = _qualified_fallback_candidate(
        tmp_path / "generation-1",
        pending_run_sha256="1" * 64,
        error_count=2,
    )
    first = materialization.select_best_qualified_fallback(
        prior=None,
        candidate_backlog_path=first_paths[0],
        candidate_report_path=first_paths[1],
        candidate_output_adjudication_path=first_paths[2],
        candidate_phase1_bundle_path=first_paths[3],
    )
    assert first is not None
    assert materialization.best_qualified_fallback_errors(first) == []

    equal_paths = _qualified_fallback_candidate(
        tmp_path / "generation-2-equal-new-error",
        pending_run_sha256="2" * 64,
        error_count=2,
    )
    equal = materialization.select_best_qualified_fallback(
        prior=first,
        candidate_backlog_path=equal_paths[0],
        candidate_report_path=equal_paths[1],
        candidate_output_adjudication_path=equal_paths[2],
        candidate_phase1_bundle_path=equal_paths[3],
    )
    assert equal == first

    improved_paths = _qualified_fallback_candidate(
        tmp_path / "generation-3-improved",
        pending_run_sha256="3" * 64,
        error_count=1,
    )
    improved = materialization.select_best_qualified_fallback(
        prior=equal,
        candidate_backlog_path=improved_paths[0],
        candidate_report_path=improved_paths[1],
        candidate_output_adjudication_path=improved_paths[2],
        candidate_phase1_bundle_path=improved_paths[3],
    )
    assert improved is not None
    assert improved["qualification_error_count"] == 1
    assert improved["superseded_fallback_content_sha256"] == first["content_sha256"]
    assert materialization.best_qualified_fallback_errors(improved) == []

    # A later failed/non-improving generation can carry the objective best forward.
    carried = materialization.select_best_qualified_fallback(
        prior=improved,
        candidate_backlog_path=None,
        candidate_report_path=None,
        candidate_output_adjudication_path=None,
        candidate_phase1_bundle_path=None,
    )
    assert carried == improved


def test_best_qualified_fallback_rejects_report_backlog_cross_transaction_binding(
    tmp_path: Path,
) -> None:
    transaction_a = _qualified_fallback_candidate(
        tmp_path / "transaction-a",
        pending_run_sha256="a" * 64,
        error_count=1,
    )
    transaction_b = _qualified_fallback_candidate(
        tmp_path / "transaction-b",
        pending_run_sha256="b" * 64,
        error_count=1,
    )

    crossed = materialization.select_best_qualified_fallback(
        prior=None,
        candidate_backlog_path=transaction_b[0],
        candidate_report_path=transaction_a[1],
        candidate_output_adjudication_path=transaction_a[2],
        candidate_phase1_bundle_path=transaction_a[3],
    )

    assert crossed is None


def test_materialization_preserves_scored_source_and_writes_bound_pending_contracts(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    stage_paths: dict[str, Path] = {}
    receipts: list[dict[str, Any]] = []
    for stage in (
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
    ):
        path = tmp_path / "runtime" / f"{stage}.json"
        document = {"schema_version": 1, "stage": stage, "items": []}
        _write(path, document)
        stage_paths[stage] = path
        receipts.append(
            {
                "stage": stage,
                "path": str(path.resolve()),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "content_sha256": _hash(document),
            }
        )

    provenance = {
        "agent_session_id": "11111111-1111-4111-8111-111111111111",
        "workspace_dir": "C:/repo",
        "exact_session_continuation": True,
        "workspace_continuity_verified": True,
        "original_author_cost_seconds": 10.0,
    }
    route: dict[str, Any] = {
        "schema_version": 1,
        "feedback_kind": "accepted_output_quality",
        "authoring_stage": "implementation_planning",
        "target_identity": "plan:old",
        "output_kind": "plan",
        "output_sha256": "a" * 64,
        "quality": "bad",
        "bad_severity": "noncritical",
        "bad_categories": ["root_cause_not_addressed"],
        "rationale": "The plan leaves the verified recurrence path unchanged.",
        "actionable_label_ids": ["label:one"],
        "correctability": "correctable",
        "route_status": "same_author_resume",
        "agent_session_id": provenance["agent_session_id"],
        "workspace_dir": provenance["workspace_dir"],
        "author_attempt_identity": {"attempt_number": 1},
        "author_provenance": provenance,
        "restart_from_stage": "implementation_planning",
        "rerun_downstream_stages": ["implementation_planning", "ticket_assembly"],
        "consumption_status": "pending_orchestration",
        "consumption_receipt": None,
    }
    route["route_sha256"] = _hash(route)
    consumption = consume_qualification_corrections(
        routes=[route],
        source_pending_run_sha256="d" * 64,
        source_adjudication_sha256="e" * 64,
        load_current_payload=lambda _route: {"plan": "old"},
        invoke_exact_author=lambda **_kwargs: AuthorRevision(
            payload={"plan": "fixed"},
            validation_errors=(),
            valid_item_keys=("plan:fixed",),
            agent_session_id=provenance["agent_session_id"],
            workspace_dir=provenance["workspace_dir"],
        ),
        rerun_downstream=lambda **_kwargs: {
            "affected_problem_ids": ["problem:one"],
            "materialized_stage_receipts": receipts,
        },
    )
    runtime = QualificationRepairRuntimeResult(
        consumption=consumption,
        stage_documents={},
        tickets=[],
        affected_problem_ids=["problem:one"],
    )
    source_backlog_path = tmp_path / "scored.backlog.json"
    atoms_path = tmp_path / "atoms.json"
    evidence_path = tmp_path / "problem_mining_evidence.json"
    case_registry_path = tmp_path / "case_registry.json"
    manifest_path = tmp_path / "manifest.json"
    adjudication_path = tmp_path / "adjudication.json"
    for path, value in (
        (atoms_path, []),
        (evidence_path, {}),
        (case_registry_path, {}),
        (manifest_path, {}),
        (adjudication_path, {}),
    ):
        _write(path, value)
    source_backlog = {
        "input": {"agent": "codex"},
        "totals": {"source_counts": {}, "severity_hint_counts": {}},
        "tickets": [],
        "artifacts": {
            "atoms_jsonl": str(atoms_path),
            "case_registry_json": str(case_registry_path),
            "six_stage_pipeline": {
                "problem_mining_evidence_json": str(evidence_path),
                "case_registry_json": str(case_registry_path),
            },
            "export_contract": {},
            "shadow_qualification": {"model_readable_roots": [str(tmp_path)]},
        },
    }
    _write(source_backlog_path, source_backlog)
    source_bytes = source_backlog_path.read_bytes()
    manifest_sha256 = sha256(manifest_path.read_bytes()).hexdigest()
    adjudication_sha256 = sha256(adjudication_path.read_bytes()).hexdigest()

    def artifact_paths(**kwargs: Any) -> dict[str, Path]:
        backlog = kwargs["backlog"]
        pipeline = backlog["artifacts"]["six_stage_pipeline"]
        return {
            "atoms": atoms_path,
            "problem_records": Path(pipeline["problem_records_json"]),
            "problem_mining_evidence": evidence_path,
            "prioritized_problems": Path(pipeline["prioritized_problems_json"]),
            "research": Path(pipeline["research_json"]),
            "solution_options": Path(pipeline["solution_options_json"]),
            "solution_selection": Path(pipeline["solution_selection_json"]),
            "change_plans": Path(pipeline["change_plans_json"]),
            "case_registry": case_registry_path,
            "qualification.output_adjudication": adjudication_path,
            "qualification.pending_run_receipt": Path(
                backlog["artifacts"]["shadow_qualification"]["pending_run_receipt_path"]
            ),
        }

    monkeypatch.setattr(materialization, "_export_artifact_paths", artifact_paths)
    original_write_backlog = materialization.write_backlog

    def injected_mid_write_failure(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("injected_mid_write_failure")

    monkeypatch.setattr(
        materialization,
        "write_backlog",
        injected_mid_write_failure,
    )
    with pytest.raises(RuntimeError, match="injected_mid_write_failure"):
        materialization.materialize_repaired_shadow_run(
            source_backlog=source_backlog,
            source_backlog_path=source_backlog_path,
            atoms=[],
            runtime=runtime,
            repo_root=tmp_path,
            repo_input=None,
            policy_config=BacklogPolicyConfig(high_surface_rules=()),
            policy_config_path=tmp_path / "policy.yaml",
            export_gate_config_path=tmp_path / "gate.yaml",
            qualification_manifest_path=manifest_path,
            qualification_manifest_sha256=manifest_sha256,
            qualification_output_adjudication_path=adjudication_path,
            qualification_output_adjudication_sha256=adjudication_sha256,
        )
    repair_parent = source_backlog_path.parent / (
        f"{source_backlog_path.stem}.qualification_repair"
    )
    assert not (repair_parent / consumption["content_sha256"]).exists()
    assert any(
        path.name.startswith(f".{consumption['content_sha256']}.failed-")
        for path in repair_parent.iterdir()
    )
    assert source_backlog_path.read_bytes() == source_bytes
    monkeypatch.setattr(materialization, "write_backlog", original_write_backlog)

    result = materialization.materialize_repaired_shadow_run(
        source_backlog=source_backlog,
        source_backlog_path=source_backlog_path,
        atoms=[],
        runtime=runtime,
        repo_root=tmp_path,
        repo_input=None,
        policy_config=BacklogPolicyConfig(high_surface_rules=()),
        policy_config_path=tmp_path / "policy.yaml",
        export_gate_config_path=tmp_path / "gate.yaml",
        qualification_manifest_path=manifest_path,
        qualification_manifest_sha256=manifest_sha256,
        qualification_output_adjudication_path=adjudication_path,
        qualification_output_adjudication_sha256=adjudication_sha256,
    )

    assert result is not None
    assert source_backlog_path.read_bytes() == source_bytes
    repaired_backlog = Path(result["repaired_backlog_path"])
    standard_pending = Path(result["pending_shadow_run_path"])
    repaired_pending = Path(result["pending_repaired_shadow_run_path"])
    assert repaired_backlog.is_file()
    assert standard_pending.is_file()
    assert repaired_pending.is_file()
    repaired_contract = json.loads(repaired_pending.read_text(encoding="utf-8"))
    assert pending_repaired_shadow_run_errors(repaired_contract) == []
    assert repaired_contract["repaired_pending_run_sha256"] == result[
        "pending_shadow_run_sha256"
    ]
    repaired_backlog_doc = json.loads(repaired_backlog.read_text(encoding="utf-8"))
    qualification = repaired_backlog_doc["artifacts"]["shadow_qualification"]
    assert qualification["labels_supplied_to_model_stages"] is True
    assert qualification["release_qualification_eligible"] is False
    assert qualification["qualification_output_adjudication_path"] != str(
        adjudication_path.resolve()
    )
    pipeline = repaired_backlog_doc["artifacts"]["six_stage_pipeline"]
    assert all(
        Path(pipeline[key]).is_relative_to(repaired_backlog.parent)
        for key in (
            "problem_records_json",
            "problem_mining_evidence_json",
            "prioritized_problems_json",
            "research_json",
            "solution_options_json",
            "solution_selection_json",
            "change_plans_json",
            "case_registry_json",
        )
    )

    repair_root = repaired_backlog.parent
    first_bytes = {
        path.relative_to(repair_root).as_posix(): path.read_bytes()
        for path in repair_root.rglob("*")
        if path.is_file()
    }
    for source_stage_path in stage_paths.values():
        source_stage_path.unlink()

    def unexpected_write(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("completed repair identity must be read, not rewritten")

    monkeypatch.setattr(materialization, "write_backlog", unexpected_write)
    monkeypatch.setattr(materialization, "write_pending_shadow_run", unexpected_write)
    replay_result = materialization.materialize_repaired_shadow_run(
        source_backlog=source_backlog,
        source_backlog_path=source_backlog_path,
        atoms=[],
        runtime=runtime,
        repo_root=tmp_path,
        repo_input=None,
        policy_config=BacklogPolicyConfig(high_surface_rules=()),
        policy_config_path=tmp_path / "policy.yaml",
        export_gate_config_path=tmp_path / "gate.yaml",
        qualification_manifest_path=manifest_path,
        qualification_manifest_sha256=manifest_sha256,
        qualification_output_adjudication_path=adjudication_path,
        qualification_output_adjudication_sha256=adjudication_sha256,
    )
    assert replay_result == result
    assert source_backlog_path.read_bytes() == source_bytes
    assert {
        path.relative_to(repair_root).as_posix(): path.read_bytes()
        for path in repair_root.rglob("*")
        if path.is_file()
    } == first_bytes
