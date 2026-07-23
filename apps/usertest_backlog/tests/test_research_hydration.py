from __future__ import annotations

import json
from pathlib import Path

import pytest
from backlog_core import (
    SOURCE_EVIDENCE_PROJECTION_VERSION,
    build_case_registry,
    build_stage_document,
    problem_case_records_from_registry,
    source_evidence_atom_projection,
    source_evidence_atom_sha256,
    source_evidence_snapshot_sha256,
    update_case_registry_stage_lineage,
)
from backlog_miner.research_evidence import BlockedReplayExecutor
from runner_core import RunnerConfig

from usertest_backlog.workflows import (
    prioritization,
    reproduction_research,
    research_hydration,
    staged,
)


def _atom(*, atom_id: str = "atom:one", detail: str = "original evidence") -> dict:
    return {
        "atom_id": atom_id,
        "source": "automated_test",
        "summary": detail,
    }


def _assignment(atom: dict) -> dict:
    return {
        "status": "complete",
        "expected_atom_ids": [atom["atom_id"]],
        "atom_receipts": [
            {
                "atom_id": atom["atom_id"],
                "atom_sha256": source_evidence_atom_sha256(atom),
                "atom_snapshot": source_evidence_atom_projection(atom),
                "source_projection_version": SOURCE_EVIDENCE_PROJECTION_VERSION,
            }
        ],
    }


def _dossier(
    *,
    problem_id: str = "problem:one",
    case_id: str = "case:one",
    atom: dict | None = None,
) -> dict:
    source_atom = atom or _atom()
    return {
        "case_id": case_id,
        "problem_id": problem_id,
        "research_schema_version": 3,
        "repo_revision": "abc123",
        "research_method": "static_diagnosis",
        "reproduction_status": "not_reproduced",
        "research_status": "evidence_sufficient",
        "implementation_performed": False,
        "writes_used": False,
        "writes_purpose": ["none"],
        "broader_class_assessment": "isolated",
        "diff_classification": "no_changes",
        "artifact_refs": [{"kind": "source", "path": "evidence.txt"}],
        "experiments": [{"experiment_id": "experiment:one"}],
        "inspected_files": ["src/example.py"],
        "inspected_symbols": ["example.run"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:one",
                "statement": "The failing path omits the required state transition.",
                "supporting_evidence": ["experiment:one"],
            }
        ],
        "root_cause_confidence": 0.9,
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": ["The live deployment was not inspected."],
        "evidence_assignment": _assignment(source_atom),
    }


def _completed_core_stage_document(
    *,
    items: list[dict],
    selected_problems: list[dict],
    agent: str = "codex",
    resolved_repo_ref: str | None = "abc123",
    artifacts: dict | None = None,
) -> dict:
    compatibility = reproduction_research.stage3_research_compatibility_contract(agent=agent)
    progress = reproduction_research._completed_prefix_checkpoint(
        selected_problems=selected_problems,
        completed_dossiers=items,
        resolved_repo_ref=resolved_repo_ref,
        compatibility_contract=compatibility,
    )
    completion = reproduction_research.completed_stage3_checkpoint(
        dossiers=items,
        fresh_research_dossier_count=len(items),
        retained_research_reused_count=0,
        compatibility_contract=compatibility,
        progress_checkpoint=progress,
    )
    return build_stage_document(
        "repro_research",
        items,
        input_meta={
            "stage_status": "completed",
            "research_compatibility": compatibility,
            "progress_checkpoint": progress,
            "completed_stage_checkpoint": completion,
        },
        artifacts=artifacts or {},
    )


def _retained_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lineage_enriched: bool = False,
) -> tuple[dict, Path]:
    artifact = (tmp_path / "retained.research.json").resolve()
    atom = _atom()
    dossier = _dossier(atom=atom)
    if lineage_enriched:
        dossier["canonical_problem_id"] = dossier["problem_id"]
        dossier["case_member_problem_ids"] = [dossier["problem_id"]]
    stage_doc = build_stage_document(
        "repro_research",
        [dossier],
        input_meta={},
        artifacts={"research_json": str(artifact)},
    )
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "evidence_atom_ids": ["atom:one"],
            }
        ],
        supporting_atoms=[atom],
    )
    registry = update_case_registry_stage_lineage(registry, stage_doc=stage_doc)
    summary = registry["cases"]["case:one"]["current_research_proof"]
    assert len(summary["full_dossier_sha256"]) == 64
    assert summary["research_stage_artifact_ref"] == {
        "name": "research_json",
        "path": str(artifact),
    }
    artifact.write_text(
        json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    record = problem_case_records_from_registry(registry)[0]
    monkeypatch.setattr(research_hydration, "assess_research_readiness", lambda _item: (True, []))
    monkeypatch.setattr(
        research_hydration,
        "verify_persisted_research_evidence",
        lambda _item: (True, []),
    )
    return record, artifact


def test_hash_bound_retained_research_hydrates_and_routes_downstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _artifact = _retained_record(tmp_path, monkeypatch)

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert errors == []
    assert dossier is not None and dossier["problem_id"] == "problem:one"
    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False
    assert route["eligible_for_downstream"] is True


def test_lineage_enriched_retained_research_uses_the_strict_contract_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _artifact = _retained_record(
        tmp_path,
        monkeypatch,
        lineage_enriched=True,
    )

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert errors == []
    assert dossier is not None
    assert dossier["canonical_problem_id"] == "problem:one"
    assert dossier["case_member_problem_ids"] == ["problem:one"]
    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False


def test_aggregate_case_evidence_hydrates_with_supporting_occurrences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = (tmp_path / "retained.aggregate.research.json").resolve()
    aggregate = _atom(atom_id="atom:aggregate", detail="14 bound occurrences")
    occurrence = _atom(atom_id="atom:occurrence", detail="one supporting occurrence")
    dossier = _dossier(atom=aggregate)
    dossier["evidence_assignment"] = {
        "status": "complete",
        "expected_atom_ids": [aggregate["atom_id"], occurrence["atom_id"]],
        "case_evidence_atom_ids": [aggregate["atom_id"]],
        "occurrence_evidence_atom_ids": [occurrence["atom_id"]],
        "atom_receipts": [
            {
                "atom_id": atom["atom_id"],
                "atom_sha256": source_evidence_atom_sha256(atom),
                "atom_snapshot": source_evidence_atom_projection(atom),
                "source_projection_version": SOURCE_EVIDENCE_PROJECTION_VERSION,
            }
            for atom in (aggregate, occurrence)
        ],
    }
    stage_doc = build_stage_document(
        "repro_research",
        [dossier],
        input_meta={},
        artifacts={"research_json": str(artifact)},
    )
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "evidence_atom_ids": [aggregate["atom_id"]],
            }
        ],
        supporting_atoms=[aggregate, occurrence],
    )
    registry = update_case_registry_stage_lineage(registry, stage_doc=stage_doc)
    artifact.write_text(
        json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    record = problem_case_records_from_registry(registry)[0]
    monkeypatch.setattr(research_hydration, "assess_research_readiness", lambda _item: (True, []))
    monkeypatch.setattr(
        research_hydration,
        "verify_persisted_research_evidence",
        lambda _item: (True, []),
    )

    hydrated, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert errors == []
    assert hydrated is not None
    assert hydrated["evidence_assignment"]["occurrence_evidence_atom_ids"] == [
        occurrence["atom_id"]
    ]
    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False


def test_occurrence_only_case_evidence_hydrates_from_signed_occurrence_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = (tmp_path / "retained.occurrences.research.json").resolve()
    atoms = [
        _atom(atom_id="atom:occurrence-one", detail="first direct observation"),
        _atom(atom_id="atom:occurrence-two", detail="second direct observation"),
    ]
    dossier = _dossier(atom=atoms[0])
    dossier["evidence_assignment"] = {
        "status": "complete",
        "expected_atom_ids": [atom["atom_id"] for atom in atoms],
        "case_evidence_atom_ids": [],
        "occurrence_evidence_atom_ids": [atom["atom_id"] for atom in atoms],
        "atom_receipts": [
            {
                "atom_id": atom["atom_id"],
                "atom_sha256": source_evidence_atom_sha256(atom),
                "atom_snapshot": source_evidence_atom_projection(atom),
                "source_projection_version": SOURCE_EVIDENCE_PROJECTION_VERSION,
            }
            for atom in atoms
        ],
    }
    stage_doc = build_stage_document(
        "repro_research",
        [dossier],
        input_meta={},
        artifacts={"research_json": str(artifact)},
    )
    registry = build_case_registry(
        [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "evidence_atom_ids": [atom["atom_id"] for atom in atoms],
            }
        ],
        supporting_atoms=atoms,
    )
    registry = update_case_registry_stage_lineage(registry, stage_doc=stage_doc)
    artifact.write_text(
        json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    record = problem_case_records_from_registry(registry)[0]
    monkeypatch.setattr(research_hydration, "assess_research_readiness", lambda _item: (True, []))
    monkeypatch.setattr(
        research_hydration,
        "verify_persisted_research_evidence",
        lambda _item: (True, []),
    )

    hydrated, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert errors == []
    assert hydrated is not None
    assert hydrated["evidence_assignment"]["case_evidence_atom_ids"] == []
    assert hydrated["evidence_assignment"]["occurrence_evidence_atom_ids"] == [
        atom["atom_id"] for atom in atoms
    ]
    assert route["research_route"] == "continue_downstream"
    assert route["selected_for_research"] is False


def test_tampered_retained_research_is_rejected_and_routed_to_fresh_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, artifact = _retained_record(tmp_path, monkeypatch)
    stage_doc = json.loads(artifact.read_text(encoding="utf-8"))
    stage_doc["items"][0]["root_cause_confidence"] = 0.1
    artifact.write_text(
        json.dumps(stage_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert dossier is None
    assert errors == ["retained_research_dossier_sha256_mismatch"]
    assert route["research_route"] == "research_update"
    assert route["selected_for_research"] is True
    assert "sha256_mismatch" in route["route_reason"]


def test_hash_matching_but_currently_unready_research_cannot_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _artifact = _retained_record(tmp_path, monkeypatch)
    monkeypatch.setattr(
        research_hydration,
        "assess_research_readiness",
        lambda _item: (False, ["material_unknown_blocks_implementation_decision"]),
    )

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert dossier is None
    assert errors[0] == "retained_research_proof_not_ready"
    assert route["research_route"] == "research_update"
    assert route["research_route"] != "resume_prior"


def test_new_source_evidence_invalidates_an_otherwise_ready_retained_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, _artifact = _retained_record(tmp_path, monkeypatch)
    new_atom = _atom(atom_id="atom:new-occurrence", detail="new occurrence")
    current_hashes = dict(record["source_evidence_atom_sha256_by_id"])
    current_hashes[new_atom["atom_id"]] = source_evidence_atom_sha256(new_atom)
    record["source_evidence_atom_ids"].append(new_atom["atom_id"])
    record["source_evidence_atom_sha256_by_id"] = current_hashes
    record["source_evidence_snapshot_sha256"] = source_evidence_snapshot_sha256(current_hashes)
    record["case_revision"] += 1

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert dossier is None
    assert errors == ["retained_research_case_revision_mismatch"]
    assert route["research_route"] == "research_update"


def test_same_atom_id_with_changed_source_bytes_routes_to_research_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, _artifact = _retained_record(tmp_path, monkeypatch)
    changed_atom = _atom(detail="corrected evidence bytes")
    changed_hashes = {"atom:one": source_evidence_atom_sha256(changed_atom)}
    record["source_evidence_atom_sha256_by_id"] = changed_hashes
    record["source_evidence_snapshot_sha256"] = source_evidence_snapshot_sha256(changed_hashes)
    record["case_revision"] += 1

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert dossier is None
    assert errors == ["retained_research_case_revision_mismatch"]
    assert route["research_route"] == "research_update"


def test_missing_current_source_snapshot_cannot_reuse_research(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, _artifact = _retained_record(tmp_path, monkeypatch)
    record["source_evidence_snapshot_complete"] = False
    record["source_evidence_snapshot_sha256"] = None

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert dossier is None
    assert errors == ["retained_research_current_source_evidence_snapshot_invalid"]
    assert route["research_route"] == "research_update"


@pytest.mark.parametrize(
    "persistence_error",
    [
        "research_planning_workspace_unavailable",
        "research_runner_artifact_changed:normalized_events.jsonl",
    ],
)
def test_missing_or_tampered_persisted_evidence_routes_to_research_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persistence_error: str,
) -> None:
    record, _artifact = _retained_record(tmp_path, monkeypatch)
    monkeypatch.setattr(
        research_hydration,
        "verify_persisted_research_evidence",
        lambda _item: (False, [persistence_error]),
    )

    dossier, errors = research_hydration.hydrate_retained_research_proof(record)
    route = prioritization._runner_research_route(record)

    assert dossier is None
    assert errors == [
        "retained_research_evidence_not_persisted",
        f"retained_research_persistence:{persistence_error}",
    ]
    assert route["research_route"] == "research_update"
    assert route["research_route"] != "resume_prior"


def test_hydrated_only_stage3_skips_fresh_research_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_dispatch(**_kwargs):
        raise AssertionError("fresh Stage-3 dispatch must not run for hydrated-only work")

    monkeypatch.setattr(reproduction_research, "run_repro_research_stage", unexpected_dispatch)
    dossier = _dossier()
    stage_doc = reproduction_research._run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=None,
        repo_ref=None,
        target_slug="target",
        selected_priority_decisions=[],
        problem_records=[],
        atoms=[],
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "compiled" / "research.json",
        out_md=tmp_path / "compiled" / "research.md",
        agent="codex",
        model=None,
        cfg=RunnerConfig(repo_root=tmp_path, runs_dir=tmp_path / "runs", agents={}, policies={}),
        dry_run=False,
        replay_timeout_seconds=300.0,
        replay_executor=BlockedReplayExecutor(reason="not_needed"),
        replay_executor_metadata={"executor": "blocked", "reason": "not_needed"},
        reused_research_dossiers=[dossier],
    )

    assert stage_doc["items"] == [dossier]
    assert stage_doc["item_count"] == 1
    assert stage_doc["input_meta"]["retained_research_reused_count"] == 1
    assert stage_doc["input_meta"]["fresh_research_dossier_count"] == 0
    assert stage_doc["input_meta"]["research_dossier_count"] == 1
    assert stage_doc["input_meta"]["evidence_sufficient_count"] == 1
    assert stage_doc["input_meta"]["blocked_case_count"] == 0
    assert stage_doc["input_meta"]["insufficient_evidence_count"] == 0
    assert stage_doc["input_meta"]["useful_research_output_count"] == 1
    assert stage_doc["input_meta"]["model_invocation_skipped"] == "all_ready_proofs_reused"


def test_stage3_merges_fresh_and_reused_dossiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = _dossier(problem_id="problem:fresh", case_id="case:fresh")
    reused = _dossier(problem_id="problem:reused", case_id="case:reused")
    calls: list[list[dict]] = []

    def fresh_dispatch(**kwargs):
        calls.append(list(kwargs["selected_problems"]))
        return _completed_core_stage_document(
            items=[fresh],
            selected_problems=list(kwargs["selected_problems"]),
            artifacts={"requests_json": str(tmp_path / "requests.json")},
        )

    monkeypatch.setattr(reproduction_research, "run_repro_research_stage", fresh_dispatch)
    stage_doc = reproduction_research._run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        repo_ref="abc123",
        target_slug="target",
        selected_priority_decisions=[
            {"problem_id": "problem:fresh", "selected_for_research": True}
        ],
        problem_records=[
            {
                "problem_id": "problem:fresh",
                "case_id": "case:fresh",
                "evidence_atom_ids": ["atom:fresh"],
                "source_evidence_atom_ids": ["atom:fresh"],
            }
        ],
        atoms=[{"atom_id": "atom:fresh"}],
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "compiled" / "research.json",
        out_md=tmp_path / "compiled" / "research.md",
        agent="codex",
        model=None,
        cfg=RunnerConfig(repo_root=tmp_path, runs_dir=tmp_path / "runs", agents={}, policies={}),
        dry_run=False,
        replay_timeout_seconds=300.0,
        replay_executor=BlockedReplayExecutor(reason="test"),
        replay_executor_metadata={"executor": "blocked", "reason": "test"},
        reused_research_dossiers=[reused],
    )

    assert len(calls) == 1 and len(calls[0]) == 1
    assert [item["problem_id"] for item in stage_doc["items"]] == [
        "problem:fresh",
        "problem:reused",
    ]
    assert stage_doc["input_meta"]["fresh_research_dossier_count"] == 1
    assert stage_doc["input_meta"]["retained_research_reused_count"] == 1
    assert stage_doc["item_count"] == 2
    assert stage_doc["input_meta"]["research_dossier_count"] == 2
    assert stage_doc["input_meta"]["evidence_sufficient_count"] == 2
    assert stage_doc["input_meta"]["blocked_case_count"] == 0
    assert stage_doc["input_meta"]["insufficient_evidence_count"] == 0
    assert stage_doc["input_meta"]["useful_research_output_count"] == 2


def test_stage3_wrapper_atomically_persists_progress_and_resumes_without_suffix_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = _dossier(problem_id="problem:fresh", case_id="case:fresh")
    reused = _dossier(problem_id="problem:reused", case_id="case:reused")
    out_json = tmp_path / "compiled" / "research.json"
    upstream = {"contract_kind": "stage3_resume_upstream", "content_sha256": "a" * 64}
    observed_progress: list[dict] = []

    class SimulatedCrash(RuntimeError):
        pass

    def crashing_dispatch(**kwargs):
        progress = build_stage_document(
            "repro_research",
            [fresh],
            input_meta={
                "stage_status": "checkpointed_progress",
                "progress_checkpoint": {"status": "checkpointed_progress"},
            },
            artifacts={},
        )
        kwargs["progress_callback"](progress)
        raise SimulatedCrash

    monkeypatch.setattr(
        reproduction_research,
        "run_repro_research_stage",
        crashing_dispatch,
    )
    common = {
        "repo_root": tmp_path,
        "repo_input": str(tmp_path),
        "repo_ref": "abc123",
        "target_slug": "target",
        "selected_priority_decisions": [
            {"problem_id": "problem:fresh", "selected_for_research": True}
        ],
        "problem_records": [
            {
                "problem_id": "problem:fresh",
                "case_id": "case:fresh",
                "evidence_atom_ids": ["atom:fresh"],
                "source_evidence_atom_ids": ["atom:fresh"],
            }
        ],
        "atoms": [{"atom_id": "atom:fresh"}],
        "artifacts_dir": tmp_path / "artifacts",
        "out_json": out_json,
        "out_md": tmp_path / "compiled" / "research.md",
        "agent": "codex",
        "model": None,
        "cfg": RunnerConfig(
            repo_root=tmp_path,
            runs_dir=tmp_path / "runs",
            agents={},
            policies={},
        ),
        "dry_run": False,
        "replay_timeout_seconds": 300.0,
        "replay_executor": BlockedReplayExecutor(reason="test"),
        "replay_executor_metadata": {"executor": "blocked", "reason": "test"},
        "reused_research_dossiers": [reused],
        "resume_upstream_contract": upstream,
        "progress_observer": lambda document: observed_progress.append(dict(document)),
    }
    with pytest.raises(SimulatedCrash):
        reproduction_research._run_repro_research_stage(**common)

    persisted = json.loads(out_json.read_text(encoding="utf-8"))
    assert persisted["items"] == [fresh]
    assert persisted["input_meta"]["resume_upstream"] == upstream
    assert observed_progress == [persisted]
    temporary_files = list(out_json.parent.glob(f".{out_json.name}.*.tmp"))
    assert temporary_files == []

    observed_resume: list[dict] = []

    def resumed_dispatch(**kwargs):
        observed_resume.append(dict(kwargs["resume_stage_document"]))
        return _completed_core_stage_document(
            items=[fresh],
            selected_problems=list(kwargs["selected_problems"]),
        )

    monkeypatch.setattr(
        reproduction_research,
        "run_repro_research_stage",
        resumed_dispatch,
    )
    completed = reproduction_research._run_repro_research_stage(
        **common,
        resume_stage_document=persisted,
    )

    assert observed_resume[0]["items"] == [fresh]
    assert completed["items"] == [fresh, reused]
    assert completed["input_meta"]["resume_upstream"] == upstream
    assert (
        staged._stage3_completed_stage(
            completed,
            expected_compatibility_contract=(
                reproduction_research.stage3_research_compatibility_contract(agent="codex")
            ),
        )
        is not None
    )
    assert (
        staged._stage3_completed_stage(
            completed,
            expected_compatibility_contract=(
                reproduction_research.stage3_research_compatibility_contract(agent="claude")
            ),
        )
        is None
    )


def test_mixed_provider_wait_resume_strips_and_reappends_exact_reused_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = _dossier(problem_id="problem:fresh", case_id="case:fresh")
    reused = _dossier(problem_id="problem:reused", case_id="case:reused")
    resume_doc = build_stage_document(
        "repro_research",
        [fresh, reused],
        input_meta={
            "stage_status": "parked_external_wait",
            "external_wait": {"status": "parked_external_wait"},
            "fresh_research_dossier_count": 1,
            "retained_research_reused_count": 1,
            "research_dossier_count": 2,
        },
        artifacts={},
    )
    observed_resume_docs: list[dict] = []

    def resumed_fresh_dispatch(**kwargs):
        observed_resume_docs.append(dict(kwargs["resume_stage_document"]))
        return _completed_core_stage_document(
            items=[fresh],
            selected_problems=list(kwargs["selected_problems"]),
        )

    monkeypatch.setattr(
        reproduction_research,
        "run_repro_research_stage",
        resumed_fresh_dispatch,
    )
    stage_doc = reproduction_research._run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(tmp_path),
        repo_ref="abc123",
        target_slug="target",
        selected_priority_decisions=[
            {"problem_id": "problem:fresh", "selected_for_research": True}
        ],
        problem_records=[
            {
                "problem_id": "problem:fresh",
                "case_id": "case:fresh",
                "evidence_atom_ids": ["atom:fresh"],
                "source_evidence_atom_ids": ["atom:fresh"],
            }
        ],
        atoms=[{"atom_id": "atom:fresh"}],
        artifacts_dir=tmp_path / "artifacts",
        out_json=tmp_path / "compiled" / "research.json",
        out_md=tmp_path / "compiled" / "research.md",
        agent="codex",
        model=None,
        cfg=RunnerConfig(repo_root=tmp_path, runs_dir=tmp_path / "runs", agents={}, policies={}),
        dry_run=False,
        replay_timeout_seconds=300.0,
        replay_executor=BlockedReplayExecutor(reason="test"),
        replay_executor_metadata={"executor": "blocked", "reason": "test"},
        resume_stage_document=resume_doc,
        reused_research_dossiers=[reused],
    )

    assert len(observed_resume_docs) == 1
    core_resume = observed_resume_docs[0]
    assert core_resume["items"] == [fresh]
    assert core_resume["item_count"] == 1
    assert core_resume["input_meta"]["retained_research_reused_count"] == 0
    assert core_resume["input_meta"]["research_dossier_count"] == 1
    assert stage_doc["items"] == [fresh, reused]
    assert stage_doc["item_count"] == 2
    assert stage_doc["input_meta"]["retained_research_reused_count"] == 1


def test_mixed_provider_wait_resume_rejects_changed_reused_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = _dossier(problem_id="problem:fresh", case_id="case:fresh")
    reused = _dossier(problem_id="problem:reused", case_id="case:reused")
    changed_reused = dict(reused)
    changed_reused["root_cause_confidence"] = 0.1
    resume_doc = build_stage_document(
        "repro_research",
        [fresh, changed_reused],
        input_meta={"stage_status": "parked_external_wait"},
        artifacts={},
    )
    monkeypatch.setattr(
        reproduction_research,
        "run_repro_research_stage",
        lambda **_kwargs: pytest.fail("core resume must not run after suffix tampering"),
    )

    with pytest.raises(ValueError, match="stage3_resume_reused_research_suffix_changed"):
        reproduction_research._run_repro_research_stage(
            repo_root=tmp_path,
            repo_input=str(tmp_path),
            repo_ref="abc123",
            target_slug="target",
            selected_priority_decisions=[
                {"problem_id": "problem:fresh", "selected_for_research": True}
            ],
            problem_records=[
                {
                    "problem_id": "problem:fresh",
                    "case_id": "case:fresh",
                    "evidence_atom_ids": ["atom:fresh"],
                    "source_evidence_atom_ids": ["atom:fresh"],
                }
            ],
            atoms=[{"atom_id": "atom:fresh"}],
            artifacts_dir=tmp_path / "artifacts",
            out_json=tmp_path / "compiled" / "research.json",
            out_md=tmp_path / "compiled" / "research.md",
            agent="codex",
            model=None,
            cfg=RunnerConfig(
                repo_root=tmp_path,
                runs_dir=tmp_path / "runs",
                agents={},
                policies={},
            ),
            dry_run=False,
            replay_timeout_seconds=300.0,
            replay_executor=BlockedReplayExecutor(reason="test"),
            replay_executor_metadata={"executor": "blocked", "reason": "test"},
            resume_stage_document=resume_doc,
            reused_research_dossiers=[reused],
        )
