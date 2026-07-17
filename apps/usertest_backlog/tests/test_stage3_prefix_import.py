from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from backlog_core import build_case_registry, build_stage_document, write_case_registry
from backlog_core.stage_contracts import research_attempt_sha256

from usertest_backlog.workflows import reproduction_research, staged


def _inputs(tmp_path: Path) -> dict[str, Any]:
    atoms = [
        {
            "atom_id": "atom:one",
            "source": "automated_test",
            "summary": "first independent failure observation",
            "text": "first independent failure observation",
        },
        {
            "atom_id": "atom:two",
            "source": "automated_test",
            "summary": "second independent failure observation",
            "text": "second independent failure observation",
        },
    ]
    problems = [
        {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "title": "First causal problem",
            "evidence_atom_ids": ["atom:one"],
            "source_evidence_atom_ids": ["atom:one"],
        },
        {
            "case_id": "case:two",
            "problem_id": "problem:two",
            "title": "Second causal problem",
            "evidence_atom_ids": ["atom:two"],
            "source_evidence_atom_ids": ["atom:two"],
        },
    ]
    decisions = [
        {
            "case_id": "case:two",
            "problem_id": "problem:two",
            "selected_for_research": True,
            "research_route": "research_now",
            "priority_bucket": "p1",
            "pre_score": 80,
        },
        {
            "case_id": "case:one",
            "problem_id": "problem:one",
            "selected_for_research": True,
            "research_route": "research_now",
            "priority_bucket": "p0",
            "pre_score": 90,
        },
    ]
    registry = build_case_registry(problems, supporting_atoms=atoms)
    paths = {
        "atoms": tmp_path / "atoms.jsonl",
        "problem_records": tmp_path / "problem_records.json",
        "problem_mining_evidence": tmp_path / "problem_mining_evidence.json",
        "prioritized_problems": tmp_path / "prioritized_problems.json",
        "case_registry": tmp_path / "case_registry.json",
    }
    paths["atoms"].write_text(
        "".join(json.dumps(atom, sort_keys=True) + "\n" for atom in atoms),
        encoding="utf-8",
    )
    paths["problem_records"].write_text(
        json.dumps(
            build_stage_document("problem_mining", problems, input_meta={}),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    paths["problem_mining_evidence"].write_text("{}\n", encoding="utf-8")
    paths["prioritized_problems"].write_text(
        json.dumps(
            build_stage_document("problem_prioritization", decisions, input_meta={}),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    write_case_registry(paths["case_registry"], registry)
    return {
        "atoms": atoms,
        "problems": problems,
        "decisions": decisions,
        "registry": registry,
        "paths": paths,
        "research_json": tmp_path / "research.json",
        "research_md": tmp_path / "research.md",
    }


def _dossier_for(
    inputs: dict[str, Any],
    *,
    problem_id: str,
    case_id: str,
) -> dict[str, Any]:
    ordered = sorted(inputs["decisions"], key=staged._research_dispatch_sort_key)
    payloads = reproduction_research._build_selected_research_payloads(
        repo_root=Path("I:/code/usertest_backlog_depth"),
        selected_priority_decisions=ordered,
        problem_records=inputs["problems"],
        atoms=inputs["atoms"],
    )
    assignment = next(
        deepcopy(payload["evidence_assignment"])
        for payload in payloads
        if payload["problem_id"] == problem_id
    )
    assignment = reproduction_research._authenticate_assignment_source_classifications(
        assignment,
        atoms=inputs["atoms"],
    )
    return {
        "case_id": case_id,
        "problem_id": problem_id,
        "research_schema_version": 3,
        "repo_revision": "revision:one",
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
                "statement": "The observed transition omits its required state update.",
                "supporting_evidence": ["experiment:one"],
            }
        ],
        "root_cause_confidence": 0.9,
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": ["Live deployment behavior was not inspected."],
        "evidence_assignment": assignment,
        "evidence_verification": {"status": "verified"},
    }


def _install_native_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def validate(
        stage_document: dict[str, Any] | None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        if stage_document is None:
            calls.append([])
            return []
        items_raw = stage_document.get("items")
        items = items_raw if isinstance(items_raw, list) else []
        calls.append([str(item.get("problem_id")) for item in items])
        for item in items:
            if item.get("_invalid_evidence") is True:
                raise ValueError("research_progress_resume_evidence_changed:invalid")
            if item.get("_tampered_attempt") is True:
                raise ValueError("research_progress_resume_attempt_changed:tampered")
        return deepcopy(items)

    monkeypatch.setattr(
        reproduction_research,
        "_resume_completed_prefix_from_stage_document",
        validate,
    )
    return calls


def _persist(
    inputs: dict[str, Any],
    dossier: dict[str, Any],
    *,
    validation_error_rescore: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return staged._persist_authenticated_stage3_single_case_prefix(
        repo_root=Path("I:/code/usertest_backlog_depth"),
        repo_input="test://source-repository",
        research_ref="revision:one",
        target_slug="usertest",
        upstream_paths=inputs["paths"],
        research_json=inputs["research_json"],
        research_md=inputs["research_md"],
        imported_dossier=dossier,
        agent="codex",
        model="gpt-5.6-terra",
        validation_error_rescore=validation_error_rescore,
    )


def test_authenticated_prefix_persists_and_resume_appends_without_rerunning_stage12(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    calls = _install_native_validator(monkeypatch)
    monkeypatch.setattr(staged, "_require_stage_model_invocation_provenance", lambda _doc: None)
    monkeypatch.setattr(
        staged,
        "_run_problem_mining_stage",
        lambda **_kwargs: pytest.fail("Stage 1 must not run during prefix import"),
    )
    monkeypatch.setattr(
        staged,
        "_run_problem_prioritization_stage",
        lambda **_kwargs: pytest.fail("Stage 2 must not run during prefix import"),
    )

    first, first_registry = _persist(
        inputs,
        _dossier_for(inputs, problem_id="problem:one", case_id="case:one"),
    )
    first_item = deepcopy(first["items"][0])
    inputs["registry"] = first_registry
    second, second_registry = _persist(
        inputs,
        _dossier_for(inputs, problem_id="problem:two", case_id="case:two"),
    )

    assert [item["problem_id"] for item in second["items"]] == [
        "problem:one",
        "problem:two",
    ]
    assert second["items"][0] == first_item
    assert any(call == ["problem:one"] for call in calls)
    markdown = inputs["research_md"].read_text(encoding="utf-8")
    assert "First causal problem" in markdown
    assert "Second causal problem" in markdown
    assert "problem:one" in markdown
    assert "problem:two" in markdown
    assert second["input_meta"]["progress_checkpoint"]["completed_prefix"]
    assert staged._stage3_completed_progress(
        second,
        expected_compatibility_contract=(
            reproduction_research.stage3_research_compatibility_contract(agent="codex")
        ),
    ) is not None
    staged._load_stage3_resume_upstream(
        stage3_document=second,
        expected_paths=inputs["paths"],
        target_slug="usertest",
        repo_input="test://source-repository",
        research_ref="revision:one",
        current_atoms=inputs["atoms"],
    )
    assert json.loads(inputs["paths"]["case_registry"].read_text(encoding="utf-8")) == (
        second_registry
    )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("wrong_order", "stage3_prefix_import_problem_order_mismatch"),
        ("wrong_case", "stage3_prefix_import_case_mismatch"),
        ("wrong_assignment", "stage3_prefix_import_assignment_mismatch"),
        ("wrong_revision", "stage3_prefix_import_revision_mismatch"),
        ("unverified", "stage3_prefix_import_evidence_not_verified"),
        ("invalid_evidence", "research_progress_resume_evidence_changed"),
        ("tampered_attempt", "research_progress_resume_attempt_changed"),
    ],
)
def test_rejected_prefix_never_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    error: str,
) -> None:
    inputs = _inputs(tmp_path)
    _install_native_validator(monkeypatch)
    monkeypatch.setattr(staged, "_require_stage_model_invocation_provenance", lambda _doc: None)
    dossier = _dossier_for(inputs, problem_id="problem:one", case_id="case:one")
    if mutation == "wrong_order":
        dossier = _dossier_for(inputs, problem_id="problem:two", case_id="case:two")
    elif mutation == "wrong_case":
        dossier["case_id"] = "case:two"
    elif mutation == "wrong_assignment":
        dossier["evidence_assignment"]["assignment_sha256"] = "0" * 64
    elif mutation == "wrong_revision":
        dossier["repo_revision"] = "revision:other"
    elif mutation == "unverified":
        dossier["evidence_verification"]["status"] = "failed"
    elif mutation == "invalid_evidence":
        dossier["_invalid_evidence"] = True
    elif mutation == "tampered_attempt":
        dossier["_tampered_attempt"] = True
    registry_before = inputs["paths"]["case_registry"].read_bytes()

    with pytest.raises(ValueError, match=error):
        _persist(inputs, dossier)

    assert not inputs["research_json"].exists()
    assert inputs["paths"]["case_registry"].read_bytes() == registry_before


def test_prior_prefix_tampering_fails_before_append_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    _install_native_validator(monkeypatch)
    monkeypatch.setattr(staged, "_require_stage_model_invocation_provenance", lambda _doc: None)
    first, first_registry = _persist(
        inputs,
        _dossier_for(inputs, problem_id="problem:one", case_id="case:one"),
    )
    first["items"][0]["_tampered_attempt"] = True
    inputs["research_json"].write_text(json.dumps(first, indent=2) + "\n", encoding="utf-8")
    inputs["registry"] = first_registry
    research_before = inputs["research_json"].read_bytes()
    registry_before = inputs["paths"]["case_registry"].read_bytes()

    with pytest.raises(ValueError, match="research_progress_resume_attempt_changed"):
        _persist(
            inputs,
            _dossier_for(inputs, problem_id="problem:two", case_id="case:two"),
        )

    assert inputs["research_json"].read_bytes() == research_before
    assert inputs["paths"]["case_registry"].read_bytes() == registry_before


def test_prefix_import_normalizes_exact_terminal_rescore_without_model_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    _install_native_validator(monkeypatch)
    monkeypatch.setattr(staged, "_require_stage_model_invocation_provenance", lambda _doc: None)
    dossier = _dossier_for(inputs, problem_id="problem:one", case_id="case:one")
    source_dossier = {"phase": "source"}
    source_attempt = {
        "attempted_dossier": source_dossier,
        "attempted_dossier_sha256": sha256(
            json.dumps(
                source_dossier,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "validation_errors_after": ["obsolete:finding"],
    }
    source_attempt["attempt_sha256"] = research_attempt_sha256(source_attempt)
    terminal_attempt = {
        "source_attempt_sha256": source_attempt["attempt_sha256"],
        "validation_errors_before": ["replacement:finding"],
        "validation_errors_after": [],
    }
    terminal_attempt["attempt_sha256"] = research_attempt_sha256(terminal_attempt)
    dossier["research_attempts"] = [source_attempt, terminal_attempt]
    receipt = tmp_path / "rescore.json"
    receipt.write_text('{"status":"authenticated"}\n', encoding="utf-8")
    rescore = {
        "schema_version": 1,
        "contract_kind": "research_validation_error_rescore",
        "source_attempt_sha256": source_attempt["attempt_sha256"],
        "source_attempted_dossier_sha256": source_attempt[
            "attempted_dossier_sha256"
        ],
        "source_validation_errors": ["obsolete:finding"],
        "replacement_validation_errors": ["replacement:finding"],
        "reason": "authenticated evaluator correction",
        "evaluator_defect_ids": ["BDS-test"],
        "rescore_receipt_path": str(receipt),
        "rescore_receipt_sha256": sha256(receipt.read_bytes()).hexdigest(),
    }

    persisted, _ = _persist(
        inputs,
        dossier,
        validation_error_rescore=rescore,
    )

    normalized = persisted["items"][0]["research_attempts"][-1]
    assert normalized["validation_error_rescore"]["authored_attempt_sha256"] == (
        terminal_attempt["attempt_sha256"]
    )
    assert normalized["attempt_sha256"] == research_attempt_sha256(normalized)
