from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from backlog_core import (
    build_operational_failure_candidates,
    eligible_problem_mining_atoms,
    extract_backlog_atoms,
    normalize_atom_lineage,
    operational_candidate_receipt_errors,
    write_case_registry,
)
from backlog_core.case_lineage import empty_case_registry
from runner_core import find_repo_root
from runner_core.pathing import slugify

import usertest_backlog.workflows.staged as staged_module
from usertest_backlog.cli import main
from usertest_backlog.workflows.derived_evidence import (
    annotate_operational_failure_candidates,
    annotate_primary_derived_evidence,
    filter_derived_history_records,
    inferred_implementation_runs_root,
    ingest_derived_evidence_records,
    with_operational_candidate_metadata,
)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _case_registry(*case_ids: str, atom_cases: dict[str, str] | None = None) -> dict[str, Any]:
    registry = empty_case_registry()
    registry["cases"] = {
        case_id: {
            "case_id": case_id,
            "canonical_problem_id": f"problem:{case_id}",
            "state": "active",
        }
        for case_id in case_ids
    }
    registry["problem_id_to_case_id"] = {f"problem:{case_id}": case_id for case_id in case_ids}
    registry["atom_id_to_case_id"] = dict(atom_cases or {})
    registry["atom_id_to_case_ids"] = {
        atom_id: [case_id] for atom_id, case_id in (atom_cases or {}).items()
    }
    return registry


def _target_contract(case_id: str, *, schema_version: int = 2) -> dict[str, Any]:
    payload = {
        "schema_version": schema_version,
        "contract_source": f"runner_stage6_target_intent_v{schema_version}",
        "case_id": case_id,
        "problem_id": f"problem:{case_id}",
        "selected_option_id": "option:root-mechanism",
        "repo_revision": "a" * 40,
        "targets": [
            {
                "action": "modify",
                "path": "src/runtime.py",
                "symbols": ["execute"],
                "change": "Correct the verified root mechanism.",
                "change_sha256": hashlib.sha256(
                    b"Correct the verified root mechanism."
                ).hexdigest(),
                "target_role": "production",
                **({"destination_path": None} if schema_version == 3 else {}),
            }
        ],
    }
    return {**payload, "contract_sha256": _canonical_sha256(payload)}


def _verified_ticket_ref(
    *,
    case_id: str,
    plan_revision_id: str,
    target_contract_schema_version: int = 2,
) -> dict[str, Any]:
    fingerprint = "1" * 16
    contract = _target_contract(case_id, schema_version=target_contract_schema_version)
    provenance = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "legacy_identity": False,
        "ticket_body_sha256": "2" * 64,
        "local_plan_sha256": "3" * 64,
        "local_plan_path": "plans/ticket.md",
        "local_plan_filename": "ticket.md",
        "verification_contract_sha256": "4" * 64,
        "target_contract": contract,
        "target_contract_sha256": contract["contract_sha256"],
        "generated_ticket": True,
    }
    binding_payload = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "ticket_body_sha256": provenance["ticket_body_sha256"],
        "local_plan_sha256": provenance["local_plan_sha256"],
        "plan_verification_contract_sha256": provenance["verification_contract_sha256"],
        "plan_target_contract_sha256": provenance["target_contract_sha256"],
        "configured_commands": ["python -m pytest -q"],
        "plan_commands": ["python -m pytest -q"],
    }
    verification_binding = {
        **binding_payload,
        "binding_sha256": _canonical_sha256(binding_payload),
    }
    return {
        "schema_version": 2,
        "fingerprint": fingerprint,
        "title": "Canonical automated ticket",
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "ticket_provenance": provenance,
        "verification_binding": verification_binding,
    }


def _derived_record(
    run_dir: Path,
    *,
    ticket_ref: dict[str, Any],
    report: dict[str, Any] | None = None,
    status: str = "complete",
    error: dict[str, Any] | None = None,
    mission_id: str = "implement_backlog_ticket_v1",
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / "ticket_ref.json", ticket_ref)
    return {
        "run_dir": str(run_dir),
        "run_rel": "target_a/20260710T010000Z/codex/0",
        "target_slug": "target_a",
        "agent": "codex",
        "status": status,
        "target_ref": {
            "repo_input": "pip:agent-adapters",
            "mission_id": mission_id,
            "execution_backend": "local",
        },
        "ticket_ref": ticket_ref,
        "report": report,
        "error": error,
        "report_validation_errors": None,
        "terminal_artifact_reads": {},
        "metrics": {"commands_executed": 999, "commands_failed": 999},
        "agent_exit_code": 1 if error is not None else 0,
    }


@pytest.mark.parametrize("target_contract_schema_version", [2, 3])
def test_direct_verified_parent_and_plan_provenance_are_authoritative(
    tmp_path: Path,
    target_contract_schema_version: int,
) -> None:
    case_id = "case:direct"
    plan_revision_id = "planrev:sha256:" + "5" * 64
    record = _derived_record(
        tmp_path / "runs" / "usertest_implement" / "target_a" / "run-direct",
        ticket_ref=_verified_ticket_ref(
            case_id=case_id,
            plan_revision_id=plan_revision_id,
            target_contract_schema_version=target_contract_schema_version,
        ),
        report={
            "confusion_points": [{"summary": "Implementation evidence retained."}],
            "suggested_changes": [
                {
                    "change": "Do not remine this proposal.",
                    "priority": "p1",
                }
            ],
        },
    )

    result = ingest_derived_evidence_records(
        [record],
        source_root=tmp_path / "runs" / "usertest_implement",
        repo_root=tmp_path,
        atom_actions={},
        case_registry=_case_registry(case_id),
    )

    assert result.atoms
    assert result.metadata["binding_record_status_counts"] == {"verified": 1}
    assert all(atom["parent_case_id"] == case_id for atom in result.atoms)
    assert all(atom["case_id"] == case_id for atom in result.atoms)
    assert all(atom["disposition"] == "supports_case" for atom in result.atoms)
    assert all(atom["disposition_status"] == "decided" for atom in result.atoms)
    assert all(atom["derived_parent_plan_revision_id"] == plan_revision_id for atom in result.atoms)
    assert all(atom["derived_source_root_kind"] == "usertest_implement" for atom in result.atoms)
    assert any(atom["evidence_class"] == "proposal" for atom in result.atoms)
    assert eligible_problem_mining_atoms(result.atoms) == []


def test_legacy_fingerprint_reconstructs_only_through_exact_atom_membership(
    tmp_path: Path,
) -> None:
    fingerprint = "abcdef0123456789"
    origin_atom_id = "primary/run:confusion_point:1"
    case_id = "case:legacy"
    record = _derived_record(
        tmp_path / "derived" / "legacy",
        ticket_ref={
            "schema_version": 1,
            "fingerprint": fingerprint,
            "title": "A title that is never used for reconstruction",
        },
        report={"confusion_points": [{"summary": "Legacy implementation evidence."}]},
    )

    result = ingest_derived_evidence_records(
        [record],
        source_root=tmp_path / "derived",
        repo_root=tmp_path,
        atom_actions={
            origin_atom_id: {
                "atom_id": origin_atom_id,
                "fingerprints": [fingerprint],
            }
        },
        case_registry=_case_registry(case_id, atom_cases={origin_atom_id: case_id}),
    )

    assert result.metadata["binding_record_status_counts"] == {"reconstructed": 1}
    assert all(atom["parent_case_id"] == case_id for atom in result.atoms)
    assert all(atom["derived_from_atom_ids"] == [origin_atom_id] for atom in result.atoms)
    receipt = result.metadata["binding_receipts"][0]
    assert receipt["matched_atom_ids"] == [origin_atom_id]
    assert receipt["authority"] == "atom_action_fingerprint_case_membership"


@pytest.mark.parametrize(
    ("atom_cases", "expected_status"),
    [
        ({}, "unavailable"),
        (
            {
                "origin:one": "case:one",
                "origin:two": "case:two",
            },
            "conflict",
        ),
    ],
)
def test_unavailable_or_conflicting_legacy_binding_is_retained_but_never_fabricated(
    tmp_path: Path,
    atom_cases: dict[str, str],
    expected_status: str,
) -> None:
    fingerprint = "9999999999999999"
    record = _derived_record(
        tmp_path / expected_status,
        ticket_ref={
            "schema_version": 1,
            "fingerprint": fingerprint,
            "title": "case:one must not be parsed from title prose",
        },
        report={
            "confusion_points": [{"summary": "Unbound evidence is retained."}],
            "suggested_changes": [{"change": "Proposed prose is not a problem."}],
        },
    )
    atom_actions = {
        atom_id: {"atom_id": atom_id, "fingerprints": [fingerprint]} for atom_id in atom_cases
    }

    result = ingest_derived_evidence_records(
        [record],
        source_root=tmp_path / "derived",
        repo_root=tmp_path,
        atom_actions=atom_actions,
        case_registry=_case_registry(*sorted(set(atom_cases.values())), atom_cases=atom_cases),
    )

    assert result.metadata["binding_record_status_counts"] == {expected_status: 1}
    assert all(atom["case_id"] is None for atom in result.atoms)
    assert all(atom["parent_case_id"] is None for atom in result.atoms)
    assert all(atom["disposition"] == "unresolved" for atom in result.atoms)
    assert all(atom["disposition_status"] == "decided" for atom in result.atoms)
    assert all(
        atom["lineage_mining_blocker"] == f"derived_parent_binding_{expected_status}"
        for atom in result.atoms
    )
    assert eligible_problem_mining_atoms(result.atoms) == []


def test_source_root_identity_deduplicates_the_same_resolved_run_and_target(
    tmp_path: Path,
) -> None:
    record = _derived_record(
        tmp_path / "derived" / "duplicate",
        ticket_ref={"schema_version": 1, "fingerprint": "8" * 16},
        report={"confusion_points": [{"summary": "One retained atom."}]},
    )

    result = ingest_derived_evidence_records(
        [record, dict(record)],
        source_root=tmp_path / "derived",
        repo_root=tmp_path,
        atom_actions={},
        case_registry=_case_registry(),
    )

    assert result.metadata["records_seen"] == 2
    assert result.metadata["records_ingested"] == 1
    assert result.metadata["duplicate_records_suppressed"] == 1
    assert len(result.records) == 1
    assert len({atom["atom_id"] for atom in result.atoms}) == len(result.atoms)


def test_scope_filter_accepts_exact_local_remote_and_owner_aliases_only(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    canonical_remote = "https://github.com/example/project.git"
    records = [
        {
            "run_rel": "local",
            "target_slug": "target_a",
            "target_ref": {"repo_input": str(repo_root)},
        },
        {
            "run_rel": "remote",
            "target_slug": "target_a",
            "target_ref": {"repo_input": canonical_remote},
        },
        {
            "run_rel": "owner",
            "target_slug": "target_a",
            "target_ref": {"repo_input": "https://example.invalid/legacy.git"},
            "ticket_ref": {"owner_repo": {"root": str(repo_root)}},
        },
        {
            "run_rel": "other-repo",
            "target_slug": "target_a",
            "target_ref": {"repo_input": "https://github.com/example/other.git"},
        },
        {
            "run_rel": "other-target",
            "target_slug": "target_b",
            "target_ref": {"repo_input": str(repo_root)},
        },
    ]

    included, metadata = filter_derived_history_records(
        records,
        target_slug="target_a",
        repo_input=str(repo_root),
        repo_root=repo_root,
        git_remote_urls=[canonical_remote],
    )

    assert [record["run_rel"] for record in included] == ["local", "remote", "owner"]
    assert metadata["records_scanned"] == 5
    assert metadata["records_included"] == 3
    assert metadata["records_excluded_repo"] == 1
    assert metadata["records_excluded_target"] == 1
    assert metadata["match_counts"] == {
        "git_remote": 1,
        "literal": 1,
        "ticket_owner_root": 1,
    }


def test_typed_runner_blocker_adds_only_one_synthetic_observation_candidate(
    tmp_path: Path,
) -> None:
    record = _derived_record(
        tmp_path / "derived" / "runner-error",
        ticket_ref={"schema_version": 1, "fingerprint": "7" * 16},
        report={
            "confusion_points": [{"summary": "Free-form derived prose."}],
            "suggested_changes": [{"change": "Free-form derived proposal."}],
        },
        status="error",
        error={"type": "RunnerSetupError", "code": "runner_bootstrap_failed"},
    )
    record["operational_failure_signals"] = [
        {
            "kind": "runner_exception",
            "phase": "setup",
            "prevented_stage": True,
            "error_type": "RunnerSetupError",
        }
    ]
    ingestion = ingest_derived_evidence_records(
        [record],
        source_root=tmp_path / "derived",
        repo_root=tmp_path,
        atom_actions={},
        case_registry=_case_registry(),
    )

    candidates = build_operational_failure_candidates(
        ingestion.records,
        ingestion.atoms,
        parent_bindings_by_run=ingestion.parent_bindings_by_run,
    )
    candidates = annotate_operational_failure_candidates(
        candidates,
        records=ingestion.records,
        source_atoms=ingestion.atoms,
        primary_source_root=tmp_path / "primary",
    )
    metadata = with_operational_candidate_metadata(ingestion.metadata, candidates)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["source"] == "operational_failure_candidate"
    assert candidate["evidence_role"] == "observation"
    assert operational_candidate_receipt_errors(candidate) == []
    assert eligible_problem_mining_atoms([*ingestion.atoms, *candidates]) == candidates
    assert metadata["operational_failure_candidates"]["count"] == 1
    assert (
        metadata["operational_failure_candidates"]["receipts"][0][
            "operational_candidate_receipt_sha256"
        ]
        == candidate["operational_candidate_receipt_sha256"]
    )


def _write_history_run(
    run_dir: Path,
    *,
    mission_id: str,
    commands_executed: int,
    commands_failed: int,
    report: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    ticket_ref: dict[str, Any] | None = None,
    repo_input: str = "pip:agent-adapters",
) -> None:
    _write_json(
        run_dir / "target_ref.json",
        {
            "repo_input": repo_input,
            "agent": "codex",
            "persona_id": "routine_operator",
            "mission_id": mission_id,
            "execution_backend": "local",
        },
    )
    _write_json(run_dir / "effective_run_spec.json", {})
    _write_json(
        run_dir / "metrics.json",
        {
            "commands_executed": commands_executed,
            "commands_failed": commands_failed,
            "event_counts": {},
        },
    )
    if report is not None:
        _write_json(run_dir / "report.json", report)
    if error is not None:
        _write_json(run_dir / "error.json", error)
    if ticket_ref is not None:
        _write_json(run_dir / "ticket_ref.json", ticket_ref)


def test_staged_backlog_reads_both_roots_without_polluting_primary_aggregates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    primary_root = tmp_path / "runs" / "usertest"
    implementation_root = inferred_implementation_runs_root(primary_root)
    _write_history_run(
        primary_root / "target_a" / "20260710T000000Z" / "codex" / "0",
        mission_id="complete_output_smoke",
        commands_executed=10,
        commands_failed=1,
        report={"confusion_points": [{"summary": "Primary observed issue."}]},
    )
    _write_history_run(
        implementation_root / "target_a" / "20260710T010000Z" / "codex" / "0",
        mission_id="implement_backlog_ticket_v1",
        commands_executed=500,
        commands_failed=500,
        report={
            "confusion_points": [{"summary": "Derived implementation evidence."}],
            "suggested_changes": [{"change": "Derived proposal must stay excluded."}],
        },
        ticket_ref={"schema_version": 1, "fingerprint": "6" * 16},
    )
    _write_history_run(
        implementation_root / "target_a" / "20260710T020000Z" / "codex" / "0",
        mission_id="implement_backlog_ticket_v1",
        commands_executed=400,
        commands_failed=400,
        error={
            "type": "RunnerSetupError",
            "code": "runner_bootstrap_failed",
            "phase": "setup",
        },
        ticket_ref={"schema_version": 1, "fingerprint": "5" * 16},
    )
    _write_history_run(
        implementation_root / "target_a" / "20260710T030000Z" / "codex" / "0",
        mission_id="implement_backlog_ticket_v1",
        commands_executed=20,
        commands_failed=1,
        error={
            "type": "AgentExecFailed",
            "exit_code": 137,
            "stderr": "apply_patch verification failed: Failed to find expected lines",
        },
        ticket_ref={"schema_version": 1, "fingerprint": "4" * 16},
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    atom_actions_path.write_text(
        yaml.safe_dump({"version": 1, "atoms": []}, sort_keys=False),
        encoding="utf-8",
    )
    observed_stage3_runs_roots: list[Path] = []
    original_stage3 = staged_module._run_repro_research_stage

    def capture_stage3_runs_root(**kwargs: Any) -> dict[str, Any]:
        observed_stage3_runs_roots.append(kwargs["cfg"].runs_dir)
        return original_stage3(**kwargs)

    monkeypatch.setattr(
        staged_module,
        "_run_repro_research_stage",
        capture_stage3_runs_root,
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(primary_root),
                "--target",
                "target_a",
                "--repo-input",
                "pip:agent-adapters",
                "--dry-run",
                "--sample-size",
                "0",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = primary_root / "target_a" / "_compiled"
    summary = json.loads((compiled / "pip-agent-adapters.backlog.json").read_text(encoding="utf-8"))
    atoms = [
        json.loads(line)
        for line in (compiled / "pip-agent-adapters.backlog.atoms.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    ingestion = summary["input"]["derived_evidence_ingestion"]

    assert summary["input"]["runs_dir"] == str(primary_root)
    assert observed_stage3_runs_roots == [primary_root]
    assert summary["input"]["implementation_runs_root"] == str(implementation_root)
    assert summary["input"]["primary_record_count"] == 1
    assert summary["input"]["derived_record_count"] == 3
    implementation_source = next(
        source for source in ingestion["source_roots"] if source["kind"] == "usertest_implement"
    )
    assert implementation_source["path"] == str(implementation_root)
    assert ingestion["binding_record_status_counts"] == {"unavailable": 3}

    aggregate = next(
        atom
        for atom in atoms
        if atom.get("source") == "aggregate_metrics" and atom.get("aggregate_kind") == "baseline"
    )
    assert aggregate["metrics"]["runs"] == 1
    assert aggregate["metrics"]["commands_executed"] == 10
    assert aggregate["metrics"]["commands_failed"] == 1
    assert aggregate["supporting_run_rels"] == ["target_a/20260710T000000Z/codex/0"]

    derived_atoms = [
        atom for atom in atoms if atom.get("derived_source_root_kind") == "usertest_implement"
    ]
    assert derived_atoms
    assert all(
        atom.get("evidence_role") in {"implementation", "verification"}
        or atom.get("source") == "operational_failure_candidate"
        for atom in derived_atoms
    )
    proposal = next(atom for atom in derived_atoms if atom.get("evidence_class") == "proposal")
    assert proposal["disposition"] == "unresolved"
    assert proposal["lineage_mining_blocker"] == "derived_parent_binding_unavailable"

    candidates = [atom for atom in atoms if atom.get("source") == "operational_failure_candidate"]
    assert len(candidates) == 1
    assert candidates[0]["operational_failure_class"] == "runner_exception"
    assert operational_candidate_receipt_errors(candidates[0]) == []
    assert ingestion["operational_failure_candidates"]["count"] == 1


def test_explicit_root_research_output_is_parented_on_the_next_cycle(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    repo_input = str(repo_root)
    primary_root = tmp_path / "owner-runs" / "usertest"
    source_run_rel = "target_a/20260710T000000Z/codex/0"
    research_run_rel = "target_a/20260710T010000Z/codex/0"
    _write_history_run(
        primary_root / Path(source_run_rel),
        mission_id="complete_output_smoke",
        commands_executed=10,
        commands_failed=1,
        report={"confusion_points": [{"summary": "Primary observed issue."}]},
        repo_input=repo_input,
    )

    research_run = primary_root / Path(research_run_rel)
    _write_history_run(
        research_run,
        mission_id="backlog_repro_research",
        commands_executed=900,
        commands_failed=800,
        report={
            "confusion_points": [{"summary": "Research evidence supports the existing parent."}],
            "suggested_changes": [
                {"change": "Research proposal cannot originate another problem."}
            ],
        },
        repo_input=repo_input,
    )
    target_ref = json.loads((research_run / "target_ref.json").read_text(encoding="utf-8"))
    case_id = "case:next-cycle-parent"
    problem_id = f"problem:{case_id}"
    origin_atom = {
        "atom_id": "atom:origin",
        "source": "run_failure_event",
        "text": "The originating observation retained by the case graph.",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment = {
        "status": "complete",
        "errors": [],
        "case_id": case_id,
        "problem_id": problem_id,
        "expected_atom_ids": ["atom:origin"],
        "atom_receipts": [
            {
                "atom_id": "atom:origin",
                "atom_sha256": _canonical_sha256(origin_atom),
                "atom_snapshot": origin_atom,
                "source_projection_version": 1,
                "origin_evidence_mode": "signed_snapshot",
                "artifact_receipts": [],
            }
        ],
    }
    assignment["assignment_sha256"] = _canonical_sha256(assignment)
    sidecar = {
        "schema_version": 1,
        "producer": "backlog_miner.research_runner",
        "target_ref_sha256": _canonical_sha256(target_ref),
        "evidence_assignment": assignment,
    }
    sidecar["sidecar_sha256"] = _canonical_sha256(sidecar)
    _write_json(research_run / "evidence_assignment.json", sidecar)

    orphan_run = (
        inferred_implementation_runs_root(primary_root)
        / "target_a"
        / "20260710T020000Z"
        / "codex"
        / "0"
    )
    _write_json(
        orphan_run / "error.json",
        {
            "type": "RuntimeError",
            "subtype": "disk_full",
            "message": "Private setup prose must not become candidate evidence.",
        },
    )
    _write_json(
        orphan_run / "run_meta.json",
        {
            "schema_version": 1,
            "run_started_utc": "2026-07-10T02:00:00Z",
            "phases": {"setup_seconds": 1.0},
        },
    )
    _write_json(
        orphan_run / "ticket_ref.json",
        {
            "schema_version": 1,
            "fingerprint": "a" * 16,
            "title": "IDEA ticket prose must remain excluded",
            "proposal": "External IDEA content is not operational evidence.",
            "export_kind": "implementation",
            "owner_repo": {"root": repo_input},
        },
    )

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    atom_actions_path.write_text(
        yaml.safe_dump({"version": 1, "atoms": []}, sort_keys=False),
        encoding="utf-8",
    )
    output_dir = tmp_path / "compiled"
    output_dir.mkdir()
    out_json = output_dir / "next-cycle.backlog.json"
    default_name = slugify(repo_input)
    write_case_registry(
        output_dir / f"{default_name}.case_registry.json",
        _case_registry(case_id),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(primary_root),
                "--target",
                "target_a",
                "--repo-input",
                repo_input,
                "--out-json",
                str(out_json),
                "--dry-run",
                "--sample-size",
                "0",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    summary = json.loads(out_json.read_text(encoding="utf-8"))
    atoms_path = output_dir / f"{default_name}.backlog.atoms.jsonl"
    atoms = [
        json.loads(line) for line in atoms_path.read_text(encoding="utf-8").splitlines() if line
    ]
    research_atoms = [
        atom
        for atom in atoms
        if atom.get("origin_run_id") == research_run_rel and atom.get("evidence_role") == "research"
    ]

    assert summary["input"]["runs_dir"] == str(primary_root)
    assert summary["input"]["primary_record_count"] == 2
    assert summary["input"]["derived_record_count"] == 1
    orphan_meta = summary["input"]["derived_evidence_ingestion"]["orphan_history_recovery"]
    assert orphan_meta["records_recovered"] == 1
    assert research_atoms
    assert all(atom["parent_case_id"] == case_id for atom in research_atoms)
    assert all(atom["case_id"] == case_id for atom in research_atoms)
    assert all(atom["disposition"] == "supports_case" for atom in research_atoms)
    assert all(atom["disposition_status"] == "decided" for atom in research_atoms)
    assert eligible_problem_mining_atoms(research_atoms) == []

    aggregate = next(
        atom
        for atom in atoms
        if atom.get("source") == "aggregate_metrics" and atom.get("aggregate_kind") == "baseline"
    )
    assert aggregate["metrics"]["runs"] == 1
    assert aggregate["metrics"]["commands_executed"] == 10
    assert aggregate["metrics"]["commands_failed"] == 1
    assert aggregate["supporting_run_rels"] == [source_run_rel]

    candidates = [atom for atom in atoms if atom.get("source") == "operational_failure_candidate"]
    assert len(candidates) == 1
    assert candidates[0]["operational_failure_class"] == "infrastructure"
    assert candidates[0]["operational_failure_phase"] == "storage"
    assert operational_candidate_receipt_errors(candidates[0]) == []
    serialized_candidate = json.dumps(candidates[0], sort_keys=True)
    assert "IDEA ticket prose" not in serialized_candidate
    assert "Private setup prose" not in serialized_candidate


def test_primary_research_runner_blocker_can_project_candidate_without_mining_prose(
    tmp_path: Path,
) -> None:
    primary_record = {
        "run_dir": str(tmp_path / "primary" / "research"),
        "run_rel": "target_a/20260710T030000Z/codex/0",
        "status": "error",
        "agent": "codex",
        "target_ref": {
            "mission_id": "backlog_repro_research",
            "execution_backend": "local",
        },
        "error": {
            "type": "ResearchHarnessError",
            "code": "research_harness_workspace_missing",
        },
        "report": {"suggested_changes": [{"change": "This prose cannot originate a case."}]},
    }
    extracted = extract_backlog_atoms([primary_record], repo_root=tmp_path)["atoms"]
    normalized = normalize_atom_lineage(
        extracted,
        case_registry=_case_registry(),
        strict_new_output=True,
    )
    primary = annotate_primary_derived_evidence(
        [primary_record],
        normalized,
        source_root=tmp_path / "primary",
        case_registry=_case_registry(),
    )
    research_atoms = primary.atoms
    candidates = build_operational_failure_candidates(
        [primary_record],
        research_atoms,
        parent_bindings_by_run=primary.parent_bindings_by_run,
    )

    assert eligible_problem_mining_atoms(research_atoms) == []
    assert len(candidates) == 1
    assert candidates[0]["source"] == "operational_failure_candidate"
    assert operational_candidate_receipt_errors(candidates[0]) == []
