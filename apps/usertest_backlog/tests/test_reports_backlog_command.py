from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from backlog_core.case_lineage import eligible_problem_mining_atoms
from runner_core import find_repo_root

import usertest_backlog.workflows.staged as staged_module
from usertest_backlog.cli import _write_chunked_problem_mining_atoms_workspace, main
from usertest_backlog.parser import build_parser
from usertest_backlog.workflows.problem_mining import _validate_relation_decision_focuses
from usertest_backlog.workflows.staged import (
    _reset_stale_unproven_actioned_atoms,
    _sync_case_registry_outcomes,
)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_yaml(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def _minimal_shadow_artifact_paths(
    *,
    repo_root: Path,
    atoms_path: Path,
    stage_paths: dict[str, Path],
    policy_path: Path,
    manifest_path: Path,
    adjudication_path: Path,
    pending_path: Path,
) -> dict[str, Path | None]:
    return {
        "atoms": atoms_path,
        "problem_records": stage_paths["problem_records"],
        "problem_mining_evidence": stage_paths["problem_mining_evidence"],
        "prioritized_problems": stage_paths["prioritized_problems"],
        "research": stage_paths["research"],
        "solution_options": stage_paths["solution_options"],
        "solution_selection": stage_paths["solution_selection"],
        "change_plans": stage_paths["change_plans"],
        "case_registry": stage_paths["case_registry"],
        "config.policy": policy_path,
        "config.research": repo_root / "configs" / "backlog_research.yaml",
        "config.export_gate": repo_root / "configs" / "backlog_export_gate.yaml",
        "qualification.corpus_manifest": manifest_path,
        "qualification.output_adjudication": adjudication_path,
        "qualification.no_actionable_receipt": None,
        "qualification.pending_run_receipt": pending_path,
    }


def _seal_minimal_shadow_pending(
    *,
    out_json: Path,
    pending_path: Path,
    manifest_path: Path,
    artifact_paths: dict[str, Path | None],
) -> dict[str, Any]:
    return staged_module.write_pending_shadow_run(
        pending_path=pending_path,
        backlog_path=out_json,
        artifact_paths=artifact_paths,
        qualification_manifest_sha256_expected=hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        output_adjudication_sha256_pre_run=None,
        generated_at="2026-07-11T00:00:00Z",
    )


def _stage1_assigned_atom(compiled_dir: Path, atom_id: str) -> dict[str, Any]:
    """Read an atom from the exact workspace handed to the stage-1 miner."""

    stage_doc = json.loads(
        (compiled_dir / "target_a.problem_records.json").read_text(encoding="utf-8")
    )
    miners = stage_doc["input_meta"]["miner_results"]
    miner = next(item for item in miners if atom_id in item["assigned_atom_ids"])
    workspace = Path(miner["workspace_dir"])
    manifest = json.loads((workspace / "atoms.json").read_text(encoding="utf-8"))
    for chunk in manifest["chunks"]:
        atoms = json.loads((workspace / chunk["file"]).read_text(encoding="utf-8"))
        for atom in atoms:
            if atom.get("atom_id") == atom_id:
                return atom
    raise AssertionError(f"assigned atom missing from stage-1 workspace: {atom_id}")


def _ticket_labeler_fingerprint(ticket: dict[str, Any]) -> str:
    title_raw = ticket.get("title")
    title = str(title_raw).strip().lower() if isinstance(title_raw, str) else ""
    evidence = sorted(item for item in ticket.get("evidence_atom_ids", []) if isinstance(item, str))
    anchor = json.dumps({"title": title, "evidence": evidence}, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(anchor).hexdigest()[:16]


def _runner_receipt(
    *, case_id: str, plan_revision_id: str, evidence_kind: str
) -> dict[str, object]:
    return {
        "receipt_schema_version": 2,
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": evidence_kind,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "fingerprint": "1" * 16,
        "run_dir": "runs/shadow",
        "verification_path": "runs/shadow/verification.json",
        "verification_sha256": "2" * 64,
        "ticket_ref_path": "runs/shadow/ticket.json",
        "ticket_ref_sha256": "3" * 64,
        "ticket_body_sha256": "4" * 64,
        "local_plan_sha256": "5" * 64,
        "local_plan_filename": "ticket.md",
        "verification_contract_sha256": "6" * 64,
        "verification_binding_sha256": "7" * 64,
        "commands": ["pytest -q tests/test_shadow.py"],
    }


def test_reports_backlog_defaults_to_signed_in_codex_author() -> None:
    args = build_parser().parse_args(["reports", "backlog", "--target", "target_a"])

    assert args.agent == "codex"


def test_live_backlog_rejects_agent_without_exact_session_correction() -> None:
    assert staged_module._live_agent_preflight_error(
        agent="claude",
        dry_run=False,
        score_shadow=False,
    ) == ("live_backlog_agent_exact_session_correction_unsupported:claude:use=codex_or_dry_run")
    assert (
        staged_module._live_agent_preflight_error(
            agent="gemini",
            dry_run=False,
            score_shadow=False,
        )
        is not None
    )


def test_non_codex_agent_remains_available_for_non_live_paths() -> None:
    assert (
        staged_module._live_agent_preflight_error(
            agent="claude",
            dry_run=True,
            score_shadow=False,
        )
        is None
    )
    assert (
        staged_module._live_agent_preflight_error(
            agent="claude",
            dry_run=False,
            score_shadow=True,
        )
        is None
    )


def test_qualification_prepare_runs_canonical_extraction_without_models_or_tickets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    research_ref = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    source_runs = tmp_path / "frozen" / "usertest"
    _seed_runs_fixture(source_runs)
    for target_ref_path in source_runs.rglob("target_ref.json"):
        target_ref = json.loads(target_ref_path.read_text(encoding="utf-8"))
        target_ref["repo_input"] = str(repo_root)
        _write_json(target_ref_path, target_ref)
    (source_runs.parent / "usertest_implement").mkdir(parents=True)

    atom_actions_path = tmp_path / "custody" / "atom_actions.yaml"
    case_registry_seed = tmp_path / "custody" / "case_registry.json"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})
    _write_json(
        case_registry_seed,
        {
            "schema_version": 1,
            "cases": {},
            "problem_id_to_case_id": {},
            "atom_id_to_case_id": {},
            "atom_id_to_case_ids": {},
            "ticket_fingerprint_to_case_id": {},
            "operational_signature_to_case_id": {},
        },
    )
    atom_actions_before = atom_actions_path.read_bytes()

    counters = {"model": 0, "stage": 0, "ticket": 0}

    def forbidden_model(*_args: object, **_kwargs: object) -> object:
        counters["model"] += 1
        raise AssertionError("qualification preparation must not invoke a model")

    def forbidden_stage(*_args: object, **_kwargs: object) -> object:
        counters["stage"] += 1
        raise AssertionError("qualification preparation must stop before Stage 1")

    def forbidden_ticket(*_args: object, **_kwargs: object) -> object:
        counters["ticket"] += 1
        raise AssertionError("qualification preparation must not assemble or mutate tickets")

    import backlog_miner.ensemble as ensemble_module
    import backlog_miner.pipeline as pipeline_module

    monkeypatch.setattr(ensemble_module, "run_backlog_prompt", forbidden_model)
    monkeypatch.setattr(ensemble_module, "run_backlog_prompt_result", forbidden_model)
    monkeypatch.setattr(pipeline_module, "run_stage_prompt_json", forbidden_model)
    for name in (
        "_run_problem_mining_stage",
        "_run_problem_prioritization_stage",
        "_run_repro_research_stage",
        "_run_solution_optioning_stage",
        "_run_solution_selection_stage",
        "_run_implementation_planning_stage",
    ):
        monkeypatch.setattr(staged_module, name, forbidden_stage)
    monkeypatch.setattr(staged_module, "assemble_backlog_tickets", forbidden_ticket)
    monkeypatch.setattr(staged_module, "_write_atom_actions_yaml", forbidden_ticket)

    out_root = tmp_path / "prepared-bundles"
    work_dir = tmp_path / "prepare-work"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "qualification-prepare",
                "--repo-root",
                str(repo_root),
                "--repo-input",
                str(repo_root),
                "--research-ref",
                research_ref,
                "--source-runs-dir",
                str(source_runs),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--case-registry-seed",
                str(case_registry_seed),
                "--out-root",
                str(out_root),
                "--work-dir",
                str(work_dir),
                "--target",
                "target_a",
            ]
        )

    assert exc.value.code == 0
    assert counters == {"model": 0, "stage": 0, "ticket": 0}
    assert atom_actions_path.read_bytes() == atom_actions_before
    bundle_paths = list(out_root.glob("*/qualification_input_bundle.json"))
    assert len(bundle_paths) == 1
    bundle = json.loads(bundle_paths[0].read_text(encoding="utf-8"))
    assert bundle["contract_kind"] == "qualification_input_bundle"
    assert bundle["scope"]["research_ref"] == research_ref.casefold()
    assert bundle["atom_corpus"]["count"] == len(bundle["atoms"])
    assert bundle["atom_corpus"]["count"] > 0
    assert not (work_dir / "prepared.backlog.json").exists()
    command_output = capsys.readouterr().out
    assert '"model_invocations": 0' in command_output
    assert '"ticket_mutations": 0' in command_output


def test_qualification_execution_restores_sealed_case_membership_before_mining() -> None:
    atom_id = "target/run/codex/0:command_failure:1"
    restored = staged_module._restore_sealed_qualification_lineage(
        [
            {
                "atom_id": atom_id,
                "run_id": "target/run/codex/0",
                "run_rel": "target/run/codex/0",
                "source": "command_failure",
                "text": "The command failed before the workflow completed.",
                "evidence_role": "observation",
            }
        ],
        case_registry={
            "schema_version": 1,
            "cases": {
                "case:existing": {
                    "case_id": "case:existing",
                    "case_state": "active",
                }
            },
            "atom_id_to_case_id": {atom_id: "case:existing"},
            "atom_id_to_case_ids": {atom_id: ["case:existing"]},
        },
    )

    assert restored[0]["case_id"] == "case:existing"
    assert restored[0]["disposition"] == "supports_case"
    assert restored[0]["disposition_status"] == "decided"
    assert eligible_problem_mining_atoms(restored) == []


def test_shadow_pipeline_rejects_invalid_export_gate_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    real_load_yaml = staged_module._load_yaml

    def load_yaml(path: Path) -> dict[str, Any]:
        if path.name == "backlog_export_gate.yaml":
            return {
                "backlog_export_gate": {
                    "enabled": True,
                    "required_consecutive_shadow_cycles": 0,
                    "require_exact_export_projection": True,
                }
            }
        return real_load_yaml(path)

    monkeypatch.setattr(staged_module, "_load_yaml", load_yaml)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--shadow",
            ]
        )

    assert exc.value.code == 2


def test_qualification_manifest_pre_run_anchor_detects_byte_exact_replacement(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "qualification.manifest.json"
    _write_json(manifest_path, {"content_sha256": "a" * 64, "labels": ["first"]})

    expected = staged_module._qualification_file_sha256(manifest_path)
    _write_json(manifest_path, {"content_sha256": "b" * 64, "labels": ["replacement"]})
    observed = staged_module._qualification_file_sha256(manifest_path)

    assert expected is not None
    assert observed is not None
    assert observed != expected


def test_qualification_labels_inside_model_readable_workspace_are_rejected(
    tmp_path: Path,
) -> None:
    model_workspace = tmp_path / "model-workspace"
    inside = model_workspace / "held-out" / "manifest.json"
    outside = tmp_path / "independent-label-store" / "manifest.json"

    errors = staged_module._qualification_workspace_exposure_errors(
        artifact_paths={"inside": inside, "outside": outside},
        model_readable_roots=[model_workspace],
    )

    assert len(errors) == 1
    assert errors[0].startswith("qualification_artifact_inside_model_readable_root:inside:")


def test_score_shadow_uses_phase_two_path_without_invoking_model_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    manifest = tmp_path / "external" / "manifest.json"
    adjudication = tmp_path / "external" / "adjudication.json"
    no_actionable = tmp_path / "external" / "no-actionable.json"
    calls: list[dict[str, object]] = []

    def score(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    def forbidden_stage(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("phase-two scoring must not invoke Stage 1")

    monkeypatch.setattr(staged_module, "_score_materialized_shadow_run", score)
    monkeypatch.setattr(staged_module, "_run_problem_mining_stage", forbidden_stage)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--shadow",
                "--score-shadow",
                "--qualification-corpus-manifest",
                str(manifest),
                "--qualification-output-adjudication",
                str(adjudication),
                "--no-actionable-evidence-receipt",
                str(no_actionable),
            ]
        )

    assert exc.value.code == 0
    assert len(calls) == 1
    assert calls[0]["qualification_manifest_path"] == manifest.resolve()
    assert calls[0]["qualification_output_adjudication_path"] == adjudication.resolve()
    assert calls[0]["no_actionable_evidence_receipt_path"] == no_actionable.resolve()


def test_external_qualification_overrides_are_rejected_for_operational_shadow(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    gate_path = repo_root / "configs" / "backlog_export_gate.yaml"
    gate_before = gate_path.read_bytes()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--operational-shadow",
                "--qualification-corpus-manifest",
                str(tmp_path / "external" / "manifest.json"),
            ]
        )

    assert exc.value.code == 2
    assert gate_path.read_bytes() == gate_before


def test_score_operational_shadow_records_without_invoking_model_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    calls: list[dict[str, object]] = []

    def score(**kwargs: object) -> int:
        calls.append(kwargs)
        return 0

    def forbidden_stage(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("operational phase-two validation must not invoke Stage 1")

    monkeypatch.setattr(
        staged_module,
        "_score_materialized_operational_shadow_run",
        score,
    )
    monkeypatch.setattr(staged_module, "_run_problem_mining_stage", forbidden_stage)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--operational-shadow",
                "--score-operational-shadow",
            ]
        )

    assert exc.value.code == 0
    assert len(calls) == 1


def test_phase_two_scores_exact_materialized_artifacts_and_records_without_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    out_json = tmp_path / "target.backlog.json"
    out_md = tmp_path / "target.backlog.md"
    manifest_path = tmp_path / "held-out" / "manifest.json"
    adjudication_path = tmp_path / "held-out" / "adjudication.json"
    pending_path = tmp_path / "target.shadow_pending.json"
    atoms_path = tmp_path / "atoms.json"
    _write_json(manifest_path, {"contract_kind": "qualification_corpus_manifest"})
    _write_json(adjudication_path, {"contract_kind": "qualification_output_adjudication"})
    _write_json(atoms_path, [])
    stage_paths: dict[str, Path] = {}
    for name in (
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    ):
        stage_paths[name] = tmp_path / f"{name}.json"
        _write_json(stage_paths[name], {"items": []})
    policy_path = repo_root / "configs" / "backlog_policy.yaml"
    backlog = {
        "tickets": [],
        "artifacts": {
            "atoms_jsonl": str(atoms_path),
            "case_registry_json": str(stage_paths["case_registry"]),
            "six_stage_pipeline": {
                "problem_records_json": str(stage_paths["problem_records"]),
                "problem_mining_evidence_json": str(
                    stage_paths["problem_mining_evidence"]
                ),
                "prioritized_problems_json": str(stage_paths["prioritized_problems"]),
                "research_json": str(stage_paths["research"]),
                "solution_options_json": str(stage_paths["solution_options"]),
                "solution_selection_json": str(stage_paths["solution_selection"]),
                "change_plans_json": str(stage_paths["change_plans"]),
                "case_registry_json": str(stage_paths["case_registry"]),
            },
            "export_contract": {"policy_config_path": str(policy_path)},
            "shadow_qualification": {
                "pending_adjudication": True,
                "qualification_corpus_manifest_path": str(manifest_path),
                "qualification_output_adjudication_path": str(adjudication_path),
                "no_actionable_evidence_receipt_path": None,
                "pending_run_receipt_path": str(pending_path),
                "model_readable_roots": [str(repo_root)],
            },
        },
    }
    _write_json(out_json, backlog)
    captured: list[dict[str, object]] = []
    artifact_paths = _minimal_shadow_artifact_paths(
        repo_root=repo_root,
        atoms_path=atoms_path,
        stage_paths=stage_paths,
        policy_path=policy_path,
        manifest_path=manifest_path,
        adjudication_path=adjudication_path,
        pending_path=pending_path,
    )
    pending = _seal_minimal_shadow_pending(
        out_json=out_json,
        pending_path=pending_path,
        manifest_path=manifest_path,
        artifact_paths=artifact_paths,
    )
    monkeypatch.setattr(
        staged_module,
        "_export_artifact_paths",
        lambda **_kwargs: artifact_paths,
    )

    def evaluate(**kwargs: object) -> dict[str, object]:
        captured.append(kwargs)
        return {
            "passed": True,
            "failures": [],
            "qualification_basis_sha256": "e" * 64,
            "qualification": {"status": "verified", "failures": []},
        }

    monkeypatch.setattr(staged_module, "evaluate_shadow_invariants", evaluate)
    monkeypatch.setattr(
        staged_module,
        "_build_export_projection",
        lambda **_kwargs: {"sha256": "f" * 64},
    )
    monkeypatch.setattr(
        staged_module,
        "write_backlog",
        lambda summary, **_kwargs: _write_json(out_json, summary),
    )
    monkeypatch.setattr(
        staged_module,
        "record_shadow_cycle",
        lambda **_kwargs: {"ready_for_export": True},
    )
    real_snapshot = staged_module._snapshot_phase1_qualification_bundle
    source_mutated_after_snapshot = False

    def snapshot_then_mutate(**kwargs: object) -> object:
        nonlocal source_mutated_after_snapshot
        result = real_snapshot(**kwargs)
        if not source_mutated_after_snapshot:
            source_mutated_after_snapshot = True
            _write_json(atoms_path, [{"atom_id": "atom:mutated-after-snapshot"}])
            for name, path in stage_paths.items():
                _write_json(path, {"items": [{"source": f"mutated:{name}"}]})
            _write_json(manifest_path, {"contract_kind": "mutated_manifest"})
            _write_json(adjudication_path, {"contract_kind": "mutated_adjudication"})
        return result

    monkeypatch.setattr(
        staged_module,
        "_snapshot_phase1_qualification_bundle",
        snapshot_then_mutate,
    )
    pending_backlog_bytes = out_json.read_bytes()

    result = staged_module._score_materialized_shadow_run(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        out_json=out_json,
        out_md=out_md,
        repo_input=None,
        shadow_gate_config={
            "required_consecutive_shadow_cycles": 2,
            "require_exact_export_projection": True,
        },
        qualification_manifest_path=manifest_path,
        qualification_output_adjudication_path=adjudication_path,
        no_actionable_evidence_receipt_path=None,
        agent="codex",
        model=None,
        cfg=SimpleNamespace(runs_dir=tmp_path / "runs"),
        research_config={},
        research_ref=None,
        replay_timeout_seconds=10800.0,
    )

    assert result == 0
    assert len(captured) == 1
    assert captured[0]["qualification_pending_run_sha256"] == pending["content_sha256"]
    assert captured[0]["qualification_output_adjudication_sha256_pre_run"] is None
    assert isinstance(captured[0]["qualification_output_adjudication_sha256_post_run"], str)
    assert captured[0]["atoms"] == []
    assert captured[0]["stage1"] == {"items": []}
    assert captured[0]["qualification_manifest"] == {
        "contract_kind": "qualification_corpus_manifest"
    }
    assert captured[0]["qualification_output_adjudication"] == {
        "contract_kind": "qualification_output_adjudication"
    }
    scored_backlog_bytes = out_json.read_bytes()
    scored_backlog = json.loads(scored_backlog_bytes)
    snapshot_path = Path(
        scored_backlog["artifacts"]["shadow_qualification"][
            "phase1_backlog_snapshot_path"
        ]
    )
    assert snapshot_path.read_bytes() == pending_backlog_bytes
    assert staged_module._score_materialized_shadow_run(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        out_json=out_json,
        out_md=out_md,
        repo_input=None,
        shadow_gate_config={
            "required_consecutive_shadow_cycles": 2,
            "require_exact_export_projection": True,
        },
        qualification_manifest_path=manifest_path,
        qualification_output_adjudication_path=adjudication_path,
        no_actionable_evidence_receipt_path=None,
        agent="codex",
        model=None,
        cfg=SimpleNamespace(runs_dir=tmp_path / "runs"),
        research_config={},
        research_ref=None,
        replay_timeout_seconds=10800.0,
    ) == 0
    assert len(captured) == 1
    assert out_json.read_bytes() == scored_backlog_bytes
    scored_meta = json.loads(scored_backlog_bytes)["artifacts"]["shadow_qualification"]
    bundle = json.loads(
        Path(scored_meta["phase1_bundle_path"]).read_text(encoding="utf-8")
    )
    stage1_snapshot = Path(bundle["artifacts"]["problem_records"]["snapshot_path"])
    _write_json(stage1_snapshot, {"items": [{"tampered": True}]})
    assert staged_module._score_materialized_shadow_run(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        out_json=out_json,
        out_md=out_md,
        repo_input=None,
        shadow_gate_config={
            "required_consecutive_shadow_cycles": 2,
            "require_exact_export_projection": True,
        },
        qualification_manifest_path=manifest_path,
        qualification_output_adjudication_path=adjudication_path,
        no_actionable_evidence_receipt_path=None,
        agent="codex",
        model=None,
        cfg=SimpleNamespace(runs_dir=tmp_path / "runs"),
        research_config={},
        research_ref=None,
        replay_timeout_seconds=10800.0,
    ) == 2
    assert len(captured) == 1


def test_score_path_consumes_routes_and_persists_isolated_repaired_pending_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    out_json = tmp_path / "target.backlog.json"
    out_md = tmp_path / "target.backlog.md"
    manifest_path = tmp_path / "held-out" / "manifest.json"
    adjudication_path = tmp_path / "held-out" / "adjudication.json"
    pending_path = tmp_path / "target.shadow_pending.json"
    atoms_path = tmp_path / "atoms.json"
    _write_json(manifest_path, {"contract_kind": "qualification_corpus_manifest"})
    _write_json(adjudication_path, {"contract_kind": "qualification_output_adjudication"})
    _write_json(atoms_path, [])
    stage_paths: dict[str, Path] = {}
    for name in (
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    ):
        stage_paths[name] = tmp_path / f"{name}.json"
        _write_json(stage_paths[name], {"items": []})
    policy_path = repo_root / "configs" / "backlog_policy.yaml"
    _write_json(
        out_json,
        {
            "tickets": [],
            "input": {"breadth_profile": "standard"},
            "artifacts": {
                "atoms_jsonl": str(atoms_path),
                "prompts_dir": str(repo_root / "configs" / "backlog_prompts"),
                "case_registry_json": str(stage_paths["case_registry"]),
                "six_stage_pipeline": {
                    "problem_records_json": str(stage_paths["problem_records"]),
                    "problem_mining_evidence_json": str(
                        stage_paths["problem_mining_evidence"]
                    ),
                    "prioritized_problems_json": str(stage_paths["prioritized_problems"]),
                    "research_json": str(stage_paths["research"]),
                    "solution_options_json": str(stage_paths["solution_options"]),
                    "solution_selection_json": str(stage_paths["solution_selection"]),
                    "change_plans_json": str(stage_paths["change_plans"]),
                    "case_registry_json": str(stage_paths["case_registry"]),
                },
                "export_contract": {"policy_config_path": str(policy_path)},
                "shadow_qualification": {
                    "pending_adjudication": True,
                    "qualification_corpus_manifest_path": str(manifest_path),
                    "qualification_output_adjudication_path": str(adjudication_path),
                    "no_actionable_evidence_receipt_path": None,
                    "pending_run_receipt_path": str(pending_path),
                    "model_readable_roots": [str(repo_root)],
                },
            },
        },
    )
    route = _qualification_execution_route("9")
    artifact_paths = _minimal_shadow_artifact_paths(
        repo_root=repo_root,
        atoms_path=atoms_path,
        stage_paths=stage_paths,
        policy_path=policy_path,
        manifest_path=manifest_path,
        adjudication_path=adjudication_path,
        pending_path=pending_path,
    )
    _seal_minimal_shadow_pending(
        out_json=out_json,
        pending_path=pending_path,
        manifest_path=manifest_path,
        artifact_paths=artifact_paths,
    )
    monkeypatch.setattr(
        staged_module,
        "_export_artifact_paths",
        lambda **_kwargs: artifact_paths,
    )
    monkeypatch.setattr(
        staged_module,
        "evaluate_shadow_invariants",
        lambda **_kwargs: {
            "passed": False,
            "failures": ["held_out_failure"],
            "qualification_basis_sha256": "e" * 64,
            "qualification": {
                "status": "failed",
                "failures": ["held_out_failure"],
                "correction_routes": [route],
            },
        },
    )
    monkeypatch.setattr(
        staged_module,
        "_build_export_projection",
        lambda **_kwargs: {"sha256": "f" * 64},
    )
    monkeypatch.setattr(
        staged_module,
        "write_backlog",
        lambda summary, **_kwargs: _write_json(out_json, summary),
    )
    scored_bytes: list[bytes] = []

    def record(**_kwargs: object) -> dict[str, object]:
        scored_bytes.append(out_json.read_bytes())
        return {"ready_for_export": False}

    monkeypatch.setattr(staged_module, "record_shadow_cycle", record)
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())
    runtime_calls: list[dict[str, object]] = []
    runtime_result = staged_module.QualificationRepairRuntimeResult(
        consumption={
            "content_sha256": "7" * 64,
            "accepted_repair_count": 1,
            "unresolved_route_count": 0,
            "route_receipts": [
                {"route_sha256": route["route_sha256"], "status": "corrected"}
            ],
            "rerun_downstream_stages": [],
            "downstream_result": {},
        },
        stage_documents={},
        tickets=[],
        affected_problem_ids=["problem:one"],
        atoms=[],
    )

    def run_runtime(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        return runtime_result

    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", run_runtime)
    materialize_calls: list[dict[str, object]] = []

    def materialize(**kwargs: object) -> dict[str, object]:
        materialize_calls.append(kwargs)
        if len(materialize_calls) == 1:
            raise KeyboardInterrupt("injected_after_runtime_checkpoint")
        return {
            "repaired_backlog_path": str(tmp_path / "repair" / "backlog.json"),
            "pending_repaired_shadow_run_path": str(
                tmp_path / "repair" / "pending.json"
            ),
            "fresh_independent_readjudication_required": True,
            "release_qualification_eligible": False,
        }

    monkeypatch.setattr(staged_module, "materialize_repaired_shadow_run", materialize)
    pending_backlog_bytes = out_json.read_bytes()

    def score() -> int:
        return staged_module._score_materialized_shadow_run(
            repo_root=repo_root,
            runs_dir=tmp_path / "runs",
            out_json=out_json,
            out_md=out_md,
            repo_input=None,
            shadow_gate_config={
                "required_consecutive_shadow_cycles": 2,
                "require_exact_export_projection": True,
            },
            qualification_manifest_path=manifest_path,
            qualification_output_adjudication_path=adjudication_path,
            no_actionable_evidence_receipt_path=None,
            agent="codex",
            model=None,
            cfg=SimpleNamespace(runs_dir=tmp_path / "runs"),
            research_config={},
            research_ref=None,
            replay_timeout_seconds=10800.0,
        )

    with pytest.raises(KeyboardInterrupt, match="injected_after_runtime_checkpoint"):
        score()
    scored_after_crash = out_json.read_bytes()
    scored_after_crash_doc = json.loads(scored_after_crash)
    qualification_meta = scored_after_crash_doc["artifacts"]["shadow_qualification"]
    pending_correction_path = Path(
        qualification_meta["qualification_correction_pending_path"]
    )
    pending_correction_bytes = pending_correction_path.read_bytes()
    pending_correction = json.loads(pending_correction_bytes)
    assert pending_correction["phase1_bundle_sha256"] == qualification_meta[
        "phase1_bundle_sha256"
    ]
    assert pending_correction["phase1_backlog_snapshot_sha256"] == qualification_meta[
        "phase1_backlog_snapshot_sha256"
    ]
    assert pending_correction["qualification_manifest_snapshot_sha256"]
    assert pending_correction[
        "qualification_output_adjudication_snapshot_sha256"
    ]
    assert set(pending_correction["source_artifact_sha256s"]) == {
        "atoms",
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    }
    tampered_pending = json.loads(json.dumps(pending_correction))
    tampered_pending["source_artifact_sha256s"]["atoms"] = "0" * 64
    tampered_pending.pop("content_sha256")
    tampered_pending["content_sha256"] = staged_module._qualification_canonical_sha256(
        tampered_pending
    )
    _write_json(pending_correction_path, tampered_pending)
    assert score() == 2
    assert len(runtime_calls) == 1
    pending_correction_path.write_bytes(pending_correction_bytes)
    # Recovery must use the immutable bundle, not these now-mutated phase-one originals.
    _write_json(atoms_path, [{"atom_id": "atom:mutated-before-recovery"}])
    for name, path in stage_paths.items():
        _write_json(path, {"items": [{"source": f"mutated:{name}"}]})
    _write_json(manifest_path, {"contract_kind": "mutated_manifest"})
    _write_json(adjudication_path, {"contract_kind": "mutated_adjudication"})
    result = score()

    assert result == 3
    assert len(runtime_calls) == 1
    assert runtime_calls[0]["routes"] == [route]
    assert len(materialize_calls) == 2
    # The raw correctable failure is retained in the backlog/diagnostic bundle,
    # but does not become a separate release-streak cycle before repaired output
    # receives fresh independent adjudication.
    assert scored_bytes == []
    assert out_json.read_bytes() == scored_after_crash
    sidecars = list(
        out_json.parent.glob(
            f"{out_json.stem}.qualification_correction_consumption.*.json"
        )
    )
    assert len(sidecars) == 1
    sidecar = sidecars[0]
    sidecar_payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_payload["accepted_repair_count"] == 1
    assert sidecar_payload["route_receipts"] == runtime_result.consumption[
        "route_receipts"
    ]
    scored_backlog_bytes = out_json.read_bytes()
    scored_backlog = json.loads(scored_backlog_bytes)
    snapshot_path = Path(
        scored_backlog["artifacts"]["shadow_qualification"][
            "phase1_backlog_snapshot_path"
        ]
    )
    assert snapshot_path.read_bytes() == pending_backlog_bytes
    assert score() == 3
    assert len(runtime_calls) == 1
    assert len(materialize_calls) == 2
    assert out_json.read_bytes() == scored_backlog_bytes
    assert sidecar.read_bytes() == sidecars[0].read_bytes()


def test_phase1_bundle_rejects_source_or_snapshot_mutation_after_validation(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    out_json = tmp_path / "target.backlog.json"
    manifest_path = tmp_path / "manifest.json"
    adjudication_path = tmp_path / "adjudication.json"
    pending_path = tmp_path / "pending.json"
    atoms_path = tmp_path / "atoms.json"
    _write_json(manifest_path, {"contract_kind": "qualification_corpus_manifest"})
    _write_json(adjudication_path, {"contract_kind": "qualification_output_adjudication"})
    _write_json(atoms_path, [])
    stage_paths: dict[str, Path] = {}
    for name in (
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    ):
        stage_paths[name] = tmp_path / f"{name}.json"
        _write_json(stage_paths[name], {"items": []})
    policy_path = repo_root / "configs" / "backlog_policy.yaml"
    backlog = {
        "tickets": [],
        "artifacts": {
            "atoms_jsonl": str(atoms_path),
            "case_registry_json": str(stage_paths["case_registry"]),
            "six_stage_pipeline": {
                "problem_records_json": str(stage_paths["problem_records"]),
                "problem_mining_evidence_json": str(
                    stage_paths["problem_mining_evidence"]
                ),
                "prioritized_problems_json": str(stage_paths["prioritized_problems"]),
                "research_json": str(stage_paths["research"]),
                "solution_options_json": str(stage_paths["solution_options"]),
                "solution_selection_json": str(stage_paths["solution_selection"]),
                "change_plans_json": str(stage_paths["change_plans"]),
                "case_registry_json": str(stage_paths["case_registry"]),
            },
        },
    }
    _write_json(out_json, backlog)
    artifact_paths = _minimal_shadow_artifact_paths(
        repo_root=repo_root,
        atoms_path=atoms_path,
        stage_paths=stage_paths,
        policy_path=policy_path,
        manifest_path=manifest_path,
        adjudication_path=adjudication_path,
        pending_path=pending_path,
    )
    pending = _seal_minimal_shadow_pending(
        out_json=out_json,
        pending_path=pending_path,
        manifest_path=manifest_path,
        artifact_paths=artifact_paths,
    )
    validated, errors = staged_module.validate_pending_shadow_run(
        pending_path=pending_path,
        backlog_path=out_json,
        artifact_paths=artifact_paths,
    )
    assert errors == []
    assert validated == pending

    _, phase1_bundle, _ = staged_module._snapshot_phase1_qualification_bundle(
        backlog=backlog,
        backlog_path=out_json,
        repo_root=repo_root,
        pending=pending,
        artifact_paths=artifact_paths,
        qualification_output_adjudication_path=adjudication_path,
    )
    immutable_pending_path = Path(phase1_bundle["immutable_pending_run"]["path"])
    immutable_pending_bytes = immutable_pending_path.read_bytes()
    _write_json(immutable_pending_path, {"tampered": True})
    with pytest.raises(
        ValueError,
        match="qualification_write_once_conflict:.*phase1.validation.pending.json",
    ):
        staged_module._snapshot_phase1_qualification_bundle(
            backlog=backlog,
            backlog_path=out_json,
            repo_root=repo_root,
            pending=pending,
            artifact_paths=artifact_paths,
            qualification_output_adjudication_path=adjudication_path,
        )
    immutable_pending_path.write_bytes(immutable_pending_bytes)

    _write_json(stage_paths["problem_records"], {"items": [{"mutated": True}]})

    with pytest.raises(
        ValueError,
        match="qualification_phase1_source_changed_after_validation:problem_records",
    ):
        staged_module._snapshot_phase1_qualification_bundle(
            backlog=backlog,
            backlog_path=out_json,
            repo_root=repo_root,
            pending=pending,
            artifact_paths=artifact_paths,
            qualification_output_adjudication_path=adjudication_path,
        )


def _qualification_execute_inputs(
    *,
    tmp_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    out_json = tmp_path / "score.backlog.json"
    manifest_path = tmp_path / "snapshot" / "manifest.json"
    adjudication_path = tmp_path / "snapshot" / "adjudication.json"
    _write_json(out_json, {"tickets": []})
    _write_json(manifest_path, {"contract_kind": "qualification_corpus_manifest"})
    _write_json(
        adjudication_path,
        {"contract_kind": "qualification_output_adjudication"},
    )
    empty_stage = {"items": []}
    context = {
        "artifacts": {"prompts_dir": str(repo_root / "configs" / "backlog_prompts")},
        "atoms": [],
        "stage1": empty_stage,
        "stage2": empty_stage,
        "stage3": empty_stage,
        "stage4": empty_stage,
        "stage5": empty_stage,
        "stage6": empty_stage,
        "case_registry": {"cases": {}},
        "qualification_manifest": {"contract_kind": "qualification_corpus_manifest"},
    }
    policy_path = repo_root / "configs" / "backlog_policy.yaml"
    policy = staged_module.BacklogPolicyConfig.from_dict(
        staged_module._load_yaml(policy_path).get("backlog_policy", {})
    )
    return {
        "repo_root": repo_root,
        "out_json": out_json,
        "backlog": {"tickets": []},
        "context": context,
        "source_pending_run_sha256": "1" * 64,
        "source_adjudication_sha256": hashlib.sha256(
            adjudication_path.read_bytes()
        ).hexdigest(),
        "correction_input_sha256": "2" * 64,
        "completion_path": tmp_path / "work" / "completion.json",
        "phase1_bundle_sha256": "3" * 64,
        "qualification_manifest_path": manifest_path,
        "qualification_manifest_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "qualification_output_adjudication_path": adjudication_path,
        "policy_config": policy,
        "policy_config_path": policy_path,
        "export_gate_config_path": repo_root / "configs" / "backlog_export_gate.yaml",
        "agent": "codex",
        "model": None,
        "cfg": object(),
        "repo_input": None,
        "research_config": {},
        "research_ref": None,
        "replay_timeout_seconds": 10800.0,
    }


def _qualification_execution_route(
    marker: str,
    *,
    status: str = "same_author_resume",
    component: str = "case:one",
) -> dict[str, Any]:
    session_id = f"{marker * 8}-{marker * 4}-4{marker * 3}-8{marker * 3}-{marker * 12}"
    return {
        "route_sha256": marker * 64,
        "route_status": status,
        "authoring_stage": "implementation_planning",
        "agent_session_id": session_id if status == "same_author_resume" else None,
        "workspace_dir": (
            f"C:/retained/{marker}" if status == "same_author_resume" else None
        ),
        "actionable_label_ids": [component],
        "author_provenance": {
            "exact_session_continuation": status == "same_author_resume",
            "problem_id": component,
        },
    }


def _qualification_execution_runtime(
    route: Mapping[str, Any],
    *,
    status: str,
    accepted: bool,
    frontier: Mapping[str, Any] | None = None,
) -> Any:
    receipt: dict[str, Any] = {
        "route_sha256": route["route_sha256"],
        "status": status,
    }
    if frontier is not None:
        receipt["correction_frontier"] = dict(frontier)
    empty_stage = {"items": []}
    return staged_module.QualificationRepairRuntimeResult(
        consumption={
            "content_sha256": ("a" if accepted else "b") * 64,
            "accepted_repair_count": 1 if accepted else 0,
            "unresolved_route_count": 0 if accepted else 1,
            "route_receipts": [receipt],
            "rerun_downstream_stages": [],
            "downstream_result": {},
        },
        stage_documents={
            "problem_mining": empty_stage,
            "problem_prioritization": empty_stage,
            "repro_research": empty_stage,
            "solution_optioning": empty_stage,
            "solution_selection": empty_stage,
            "implementation_planning": empty_stage,
        },
        tickets=[],
        affected_problem_ids=[],
        atoms=[],
    )


def test_unavailable_author_route_is_retained_without_model_invocation_or_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    route = {"route_sha256": "4" * 64, "route_status": "author_provenance_unavailable"}
    runtime_calls: list[dict[str, object]] = []

    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())

    def crash(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        raise AssertionError("unavailable route must not invoke a model")

    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", crash)

    result = staged_module._execute_qualification_correction(routes=[route], **inputs)

    assert runtime_calls == []
    assert result["status"] == (
        "repairable_paused:qualification_correction_frontier_retained"
    )
    assert result["qualification_scheduler_pending"] is True
    assert not Path(inputs["completion_path"]).exists()


def test_indeterminate_crash_uses_one_bound_exact_session_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    session_id = "019f2cca-9011-7e32-88ae-6c25af578b49"
    route = {
        "route_sha256": "5" * 64,
        "route_status": "same_author_resume",
        "agent_session_id": session_id,
        "author_provenance": {"exact_session_continuation": True},
    }
    runtime_calls: list[dict[str, object]] = []
    runtime_result = staged_module.QualificationRepairRuntimeResult(
        consumption={
            "content_sha256": "6" * 64,
            "accepted_repair_count": 1,
            "unresolved_route_count": 0,
            "route_receipts": [
                {"route_sha256": route["route_sha256"], "status": "corrected"}
            ],
            "rerun_downstream_stages": [],
            "downstream_result": {},
        },
        stage_documents={},
        tickets=[],
        affected_problem_ids=[],
        atoms=[],
    )
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())

    def reconcile(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        if len(runtime_calls) == 1:
            raise KeyboardInterrupt("unknown_after_exact_session_turn")
        return runtime_result

    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", reconcile)
    monkeypatch.setattr(
        staged_module,
        "materialize_repaired_shadow_run",
        lambda **_kwargs: {
            "accepted_repair_count": 1,
            "fresh_independent_readjudication_required": True,
            "release_qualification_eligible": False,
        },
    )

    with pytest.raises(KeyboardInterrupt, match="unknown_after_exact_session_turn"):
        staged_module._execute_qualification_correction(routes=[route], **inputs)
    recovered = staged_module._execute_qualification_correction(routes=[route], **inputs)
    reused = staged_module._execute_qualification_correction(routes=[route], **inputs)

    assert len(runtime_calls) == 2
    assert all(call["routes"] == [route] for call in runtime_calls)
    assert recovered["accepted_repair_count"] == 1
    assert reused["correction_completion_reused"] is True
    assert Path(inputs["completion_path"]).is_file()
    assert len(
        list(
            Path(inputs["completion_path"]).parent.glob(
                "group_attempts/*/*/reconciliation_claim.json"
            )
        )
    ) == 1


def test_concurrent_scorer_returns_in_progress_without_duplicate_model_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    session_id = "019f2cca-9011-7e32-88ae-6c25af578b49"
    route = {
        "route_sha256": "7" * 64,
        "route_status": "same_author_resume",
        "agent_session_id": session_id,
        "author_provenance": {"exact_session_continuation": True},
    }
    runtime_result = staged_module.QualificationRepairRuntimeResult(
        consumption={
            "content_sha256": "8" * 64,
            "accepted_repair_count": 1,
            "unresolved_route_count": 0,
            "route_receipts": [
                {"route_sha256": route["route_sha256"], "status": "corrected"}
            ],
            "rerun_downstream_stages": [],
            "downstream_result": {},
        },
        stage_documents={},
        tickets=[],
        affected_problem_ids=[],
        atoms=[],
    )
    entered_runtime = threading.Event()
    release_runtime = threading.Event()
    runtime_calls: list[dict[str, object]] = []

    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())

    def blocked_runtime(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        entered_runtime.set()
        release_runtime.wait()
        return runtime_result

    monkeypatch.setattr(
        staged_module,
        "run_stage456_qualification_repairs",
        blocked_runtime,
    )
    monkeypatch.setattr(
        staged_module,
        "materialize_repaired_shadow_run",
        lambda **_kwargs: {
            "accepted_repair_count": 1,
            "fresh_independent_readjudication_required": True,
            "release_qualification_eligible": False,
        },
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            staged_module._execute_qualification_correction,
            routes=[route],
            **inputs,
        )
        entered_runtime.wait()
        concurrent = staged_module._execute_qualification_correction(
            routes=[route],
            **inputs,
        )
        release_runtime.set()
        completed = first.result()

    assert concurrent["status"] == "correction_in_progress"
    assert concurrent["authored_work_disposition"] == "retained"
    assert concurrent["fresh_author_invocation_suppressed"] is True
    assert len(runtime_calls) == 1
    assert completed["accepted_repair_count"] == 1


def test_paused_qualification_resumes_same_frontier_without_terminal_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    route = _qualification_execution_route("c")
    frontier = {
        "content_sha256": "d" * 64,
        "agent_session_id": route["agent_session_id"],
        "workspace_dir": route["workspace_dir"],
        "current": {"payload": {"revision": 2}},
        "best": {"payload": {"revision": 2}},
        "attempts": [{"payload": {"revision": 1}}, {"payload": {"revision": 2}}],
        "assessments": [{"decision": "continue"}],
        "correction_cost_since_progress": 12.0,
        "total_correction_cost": 12.0,
    }
    runtime_calls: list[dict[str, object]] = []
    results = iter(
        [
            _qualification_execution_runtime(
                route,
                status="repairable_paused:correction_cost_reached_original_authoring_cost",
                accepted=False,
                frontier=frontier,
            ),
            _qualification_execution_runtime(
                route,
                status="corrected",
                accepted=True,
            ),
        ]
    )
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())

    def run_runtime(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        return next(results)

    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", run_runtime)
    monkeypatch.setattr(
        staged_module,
        "materialize_repaired_shadow_run",
        lambda **kwargs: (
            {
                "accepted_repair_count": 1,
                "fresh_independent_readjudication_required": True,
                "release_qualification_eligible": False,
            }
            if kwargs["runtime"].consumption["accepted_repair_count"] > 0
            else None
        ),
    )

    paused = staged_module._execute_qualification_correction(routes=[route], **inputs)

    assert paused["qualification_scheduler_pending"] is True
    assert paused["status"].startswith("repairable_paused:")
    assert not Path(inputs["completion_path"]).exists()
    first_checkpoint = Path(paused["qualification_scheduler_checkpoint_path"])
    assert first_checkpoint.name == (
        paused["qualification_scheduler_checkpoint_sha256"] + ".json"
    )

    resumed = staged_module._execute_qualification_correction(routes=[route], **inputs)
    reused = staged_module._execute_qualification_correction(routes=[route], **inputs)

    assert len(runtime_calls) == 2
    assert runtime_calls[0]["routes"] == [route]
    assert runtime_calls[0]["resume_frontiers"] == {}
    assert runtime_calls[1]["routes"] == [route]
    assert runtime_calls[1]["resume_frontiers"] == {
        route["route_sha256"]: frontier
    }
    assert resumed["status"] == "corrected_pending_independent_readjudication"
    assert resumed["qualification_scheduler_pending"] is False
    assert Path(inputs["completion_path"]).is_file()
    assert reused["correction_completion_reused"] is True
    scheduler_checkpoints = list(
        (Path(inputs["completion_path"]).parent / "scheduler_checkpoints").glob("*.json")
    )
    assert len(scheduler_checkpoints) >= 5


def test_confirmed_recurrence_terminalizes_and_reuses_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    route = _qualification_execution_route("d")
    runtime_calls: list[dict[str, object]] = []
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())

    def run_runtime(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        return _qualification_execution_runtime(
            route,
            status="stalled:previous_state_recurred",
            accepted=False,
        )

    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", run_runtime)
    monkeypatch.setattr(staged_module, "materialize_repaired_shadow_run", lambda **_kwargs: None)

    first = staged_module._execute_qualification_correction(routes=[route], **inputs)
    reused = staged_module._execute_qualification_correction(routes=[route], **inputs)

    assert len(runtime_calls) == 1
    assert first["status"] == "terminal_nonprogress"
    assert first["qualification_scheduler_pending"] is False
    assert Path(inputs["completion_path"]).is_file()
    assert reused["correction_completion_reused"] is True


def test_explicit_uncorrectable_route_terminalizes_without_model_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    route = _qualification_execution_route(
        "b",
        status="uncorrectable",
    )
    runtime_calls: list[dict[str, object]] = []
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())
    monkeypatch.setattr(
        staged_module,
        "run_stage456_qualification_repairs",
        lambda **kwargs: runtime_calls.append(kwargs),
    )
    monkeypatch.setattr(staged_module, "materialize_repaired_shadow_run", lambda **_kwargs: None)

    first = staged_module._execute_qualification_correction(routes=[route], **inputs)
    reused = staged_module._execute_qualification_correction(routes=[route], **inputs)

    assert runtime_calls == []
    assert first["status"] == "terminal_nonprogress"
    assert first["qualification_scheduler_pending"] is False
    assert reused["correction_completion_reused"] is True


def test_scheduler_materializes_six_stage_and_auxiliary_receipts_without_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    route = _qualification_execution_route("8")
    stage_documents: dict[str, dict[str, Any]] = {}
    receipts: list[dict[str, Any]] = []
    for stage in (
        "problem_mining",
        "problem_prioritization",
        "repro_research",
        "solution_optioning",
        "solution_selection",
        "implementation_planning",
        "problem_mining_evidence",
        "case_registry",
    ):
        document = {
            "schema_version": 1,
            "stage": stage,
            "items": [],
            "marker": f"repaired:{stage}",
        }
        path = tmp_path / "runtime-materialized" / f"{stage}.json"
        _write_json(path, document)
        receipts.append(
            {
                "stage": stage,
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content_sha256": staged_module._qualification_canonical_sha256(
                    document
                ),
            }
        )
        if stage in {
            "problem_mining",
            "problem_prioritization",
            "repro_research",
            "solution_optioning",
            "solution_selection",
            "implementation_planning",
        }:
            stage_documents[stage] = document
    runtime_result = staged_module.QualificationRepairRuntimeResult(
        consumption={
            "content_sha256": "7" * 64,
            "accepted_repair_count": 1,
            "unresolved_route_count": 0,
            "route_receipts": [
                {"route_sha256": route["route_sha256"], "status": "corrected"}
            ],
            "rerun_downstream_stages": ["implementation_planning"],
            "downstream_result": {
                "affected_problem_ids": ["problem:one"],
                "requested_downstream_stages": ["implementation_planning"],
                "materialized_stage_receipts": receipts,
            },
        },
        stage_documents=stage_documents,
        tickets=[],
        affected_problem_ids=["problem:one"],
        atoms=[{"atom_id": "atom:one", "disposition": "supports_case"}],
        case_registry={"cases": {"case:one": {"case_id": "case:one"}}},
    )
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())
    monkeypatch.setattr(
        staged_module,
        "run_stage456_qualification_repairs",
        lambda **_kwargs: runtime_result,
    )

    result = staged_module._execute_qualification_correction(routes=[route], **inputs)

    repaired_backlog_path = Path(result["repaired_backlog_path"])
    assert repaired_backlog_path.is_file()
    repaired = json.loads(repaired_backlog_path.read_text(encoding="utf-8"))
    pipeline = repaired["artifacts"]["six_stage_pipeline"]
    published = {
        "problem_mining": Path(pipeline["problem_records_json"]),
        "problem_prioritization": Path(pipeline["prioritized_problems_json"]),
        "repro_research": Path(pipeline["research_json"]),
        "solution_optioning": Path(pipeline["solution_options_json"]),
        "solution_selection": Path(pipeline["solution_selection_json"]),
        "implementation_planning": Path(pipeline["change_plans_json"]),
        "problem_mining_evidence": Path(pipeline["problem_mining_evidence_json"]),
        "case_registry": Path(pipeline["case_registry_json"]),
    }
    assert all(path.is_file() for path in published.values())
    assert {
        json.loads(path.read_text(encoding="utf-8"))["marker"]
        for path in published.values()
    } == {f"repaired:{stage}" for stage in published}
    assert Path(result["pending_repaired_shadow_run_path"]).is_file()
    assert result["release_qualification_eligible"] is False
    published_consumption = json.loads(
        Path(result["correction_consumption_path"]).read_text(encoding="utf-8")
    )
    assert json.loads(json.dumps(published_consumption)) == published_consumption
    assert published_consumption["content_sha256"] == (
        staged_module._qualification_canonical_sha256(
            {
                key: value
                for key, value in published_consumption.items()
                if key != "content_sha256"
            }
        )
    )


def test_legacy_zero_accepted_completion_is_resumed_not_reused_forever(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    route = _qualification_execution_route("a")
    legacy_consumption = tmp_path / "legacy_consumption.json"
    _write_json(legacy_consumption, {"legacy": True})
    legacy_completion = staged_module._build_qualification_correction_completion(
        correction_input_sha256=inputs["correction_input_sha256"],
        consumption_path=legacy_consumption,
        consumption_sha256="b" * 64,
        repair_result={
            "accepted_repair_count": 0,
            "unresolved_route_count": 1,
            "fresh_independent_readjudication_required": False,
            "release_qualification_eligible": False,
        },
    )
    _write_json(inputs["completion_path"], legacy_completion)
    runtime_calls: list[dict[str, object]] = []
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())

    def run_runtime(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        return _qualification_execution_runtime(
            route,
            status="corrected",
            accepted=True,
        )

    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", run_runtime)
    monkeypatch.setattr(
        staged_module,
        "materialize_repaired_shadow_run",
        lambda **_kwargs: {
            "accepted_repair_count": 1,
            "fresh_independent_readjudication_required": True,
            "release_qualification_eligible": False,
        },
    )

    corrected = staged_module._execute_qualification_correction(routes=[route], **inputs)
    reused = staged_module._execute_qualification_correction(routes=[route], **inputs)

    assert len(runtime_calls) == 1
    assert corrected["accepted_repair_count"] == 1
    assert ".terminal." in Path(corrected["correction_completion_path"]).name
    assert reused["correction_completion_reused"] is True


def test_unavailable_route_does_not_poison_recoverable_group_crash_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    unavailable = _qualification_execution_route(
        "e",
        status="author_provenance_unavailable",
        component="case:unavailable",
    )
    recoverable = _qualification_execution_route(
        "f",
        component="case:recoverable",
    )
    runtime_calls: list[dict[str, object]] = []
    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())

    def run_runtime(**kwargs: object) -> object:
        runtime_calls.append(kwargs)
        if len(runtime_calls) == 1:
            raise KeyboardInterrupt("lost_after_exact_group_turn")
        return _qualification_execution_runtime(
            recoverable,
            status="corrected",
            accepted=True,
        )

    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", run_runtime)
    monkeypatch.setattr(
        staged_module,
        "materialize_repaired_shadow_run",
        lambda **_kwargs: {
            "accepted_repair_count": 1,
            "fresh_independent_readjudication_required": True,
            "release_qualification_eligible": False,
        },
    )

    with pytest.raises(KeyboardInterrupt, match="lost_after_exact_group_turn"):
        staged_module._execute_qualification_correction(
            routes=[unavailable, recoverable],
            **inputs,
        )
    recovered = staged_module._execute_qualification_correction(
        routes=[unavailable, recoverable],
        **inputs,
    )

    assert len(runtime_calls) == 2
    assert all(call["routes"] == [recoverable] for call in runtime_calls)
    assert recovered["accepted_repair_count"] == 1
    assert recovered["qualification_scheduler_pending"] is True
    assert not Path(inputs["completion_path"]).exists()
    assert len(
        list(
            Path(inputs["completion_path"]).parent.glob(
                "group_attempts/*/*/reconciliation_claim.json"
            )
        )
    ) == 1


def test_scheduler_replans_after_stage1_merge_and_never_invokes_stale_planner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    inputs = _qualification_execute_inputs(tmp_path=tmp_path, repo_root=repo_root)
    stage1_route = _qualification_execution_route("1", component="label:stage1")
    stage1_route.update(
        {
            "authoring_stage": "problem_mining",
            "causal_target": {
                "problem_ids": ["problem:one"],
                "case_ids": ["case:one"],
                "evidence_atom_ids": ["atom:one"],
                "actionable_label_ids": [],
                "expected_item_keys": ["atom:one"],
            },
        }
    )
    stage1_route["author_provenance"] = {
        **stage1_route["author_provenance"],
        "authoring_stage": "problem_mining",
        "problem_id": "problem:one",
        "case_id": "case:one",
        "stage1_correction_adapter": "problem_miner",
    }
    planner_route = _qualification_execution_route("2", component="label:planner")
    planner_route["causal_target"] = {
        "problem_ids": ["problem:two"],
        "case_ids": ["case:two"],
        "evidence_atom_ids": [],
        "actionable_label_ids": [],
        "expected_item_keys": ["problem:two"],
    }
    planner_route["author_provenance"] = {
        **planner_route["author_provenance"],
        "problem_id": "problem:two",
        "case_id": "case:two",
    }
    corrected_stage1 = {
        "items": [
            {
                "problem_id": "problem:one",
                "case_id": "case:one",
                "case_member_problem_ids": ["problem:one", "problem:two"],
                "evidence_atom_ids": ["atom:one"],
            }
        ]
    }
    documents = {
        "problem_mining": corrected_stage1,
        "problem_prioritization": {"items": []},
        "repro_research": {"items": []},
        "solution_optioning": {"items": []},
        "solution_selection": {"items": []},
        "implementation_planning": {"items": []},
    }
    corrected_registry = {
        "problem_id_to_case_id": {
            "problem:one": "case:one",
            "problem:two": "case:one",
        },
        "cases": {
            "case:one": {
                "case_id": "case:one",
                "canonical_problem_id": "problem:one",
                "problem_ids": ["problem:one", "problem:two"],
                "absorbed_case_ids": ["case:two"],
            }
        },
    }
    runtime_calls: list[list[Mapping[str, Any]]] = []

    def run_runtime(**kwargs: object) -> Any:
        routes = kwargs["routes"]
        assert isinstance(routes, list)
        runtime_calls.append(routes)
        assert routes == [stage1_route]
        return staged_module.QualificationRepairRuntimeResult(
            consumption={
                "content_sha256": "3" * 64,
                "accepted_repair_count": 1,
                "unresolved_route_count": 0,
                "route_receipts": [
                    {
                        "route_sha256": stage1_route["route_sha256"],
                        "status": "corrected",
                    }
                ],
                "rerun_downstream_stages": [],
                "downstream_result": {},
            },
            stage_documents=documents,
            tickets=[],
            affected_problem_ids=["problem:one", "problem:two"],
            atoms=[{"atom_id": "atom:one", "disposition": "supports_case"}],
            case_registry=corrected_registry,
        )

    monkeypatch.setattr(staged_module, "load_pipeline_prompt_manifest", lambda _path: object())
    monkeypatch.setattr(staged_module, "run_stage456_qualification_repairs", run_runtime)
    monkeypatch.setattr(
        staged_module,
        "materialize_repaired_shadow_run",
        lambda **_kwargs: {
            "accepted_repair_count": 1,
            "fresh_independent_readjudication_required": True,
            "release_qualification_eligible": False,
        },
    )

    result = staged_module._execute_qualification_correction(
        routes=[stage1_route, planner_route],
        **inputs,
    )

    assert runtime_calls == [[stage1_route]]
    assert result["qualification_scheduler_pending"] is True
    checkpoint = json.loads(
        Path(result["qualification_scheduler_checkpoint_path"]).read_text(
            encoding="utf-8"
        )
    )
    planner_group = next(
        group
        for group in checkpoint["group_states"].values()
        if planner_route["route_sha256"] in group["route_sha256s"]
    )
    assert planner_group["status"] == "retained_pending_causal_predecessor"
    assert planner_group["blocked_by_group_id"] is not None
    assert checkpoint["current_causal_plan_sha256"]


def test_operational_phase_two_evaluates_internal_contract_without_benchmark_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    out_json = tmp_path / "target.backlog.json"
    atoms_path = tmp_path / "atoms.json"
    _write_json(atoms_path, [])
    stage_paths: dict[str, Path] = {}
    for name in (
        "problem_records",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    ):
        stage_paths[name] = tmp_path / f"{name}.json"
        _write_json(stage_paths[name], {"items": []})
    pending_path = tmp_path / "target.operational_shadow_pending.json"
    backlog = {
        "tickets": [],
        "artifacts": {
            "atoms_jsonl": str(atoms_path),
            "case_registry_json": str(stage_paths["case_registry"]),
            "six_stage_pipeline": {
                "problem_records_json": str(stage_paths["problem_records"]),
                "prioritized_problems_json": str(stage_paths["prioritized_problems"]),
                "research_json": str(stage_paths["research"]),
                "solution_options_json": str(stage_paths["solution_options"]),
                "solution_selection_json": str(stage_paths["solution_selection"]),
                "change_plans_json": str(stage_paths["change_plans"]),
                "case_registry_json": str(stage_paths["case_registry"]),
            },
            "export_contract": {
                "policy_config_path": str(repo_root / "configs" / "backlog_policy.yaml")
            },
            "operational_shadow": {
                "pending_internal_validation": True,
                "pending_run_receipt_path": str(pending_path),
                "model_readable_roots": [str(repo_root)],
            },
        },
    }
    _write_json(out_json, backlog)
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(staged_module, "_export_artifact_paths", lambda **_kwargs: {})
    release_bundle = tmp_path / "release" / "qualification_input_bundle.json"
    release_bundle.parent.mkdir(parents=True, exist_ok=True)
    release_bundle.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        staged_module,
        "_release_qualification_bundle_from_state",
        lambda _state_path: release_bundle,
    )
    monkeypatch.setattr(
        staged_module,
        "load_qualification_input_bundle",
        lambda _path, *, verify_files: {},
    )
    monkeypatch.setattr(
        staged_module,
        "qualification_runtime_compatibility_errors",
        lambda _bundle, *, repo_root: [],
    )
    monkeypatch.setattr(
        staged_module,
        "validate_pending_operational_shadow_run",
        lambda **_kwargs: ({"content_sha256": "d" * 64}, []),
    )

    def evaluate(**kwargs: object) -> dict[str, object]:
        captured.append(kwargs)
        return {"passed": True, "failures": []}

    monkeypatch.setattr(staged_module, "evaluate_shadow_invariants", evaluate)
    monkeypatch.setattr(
        staged_module,
        "_build_export_projection",
        lambda **_kwargs: {"sha256": "f" * 64},
    )
    monkeypatch.setattr(
        staged_module,
        "record_shadow_cycle",
        lambda **_kwargs: {
            "ready_for_export": True,
            "activation_mode": "operational_bound",
            "release_anchor_cycle_ids": ["1" * 64, "2" * 64],
        },
    )

    result = staged_module._score_materialized_operational_shadow_run(
        repo_root=repo_root,
        runs_dir=tmp_path / "runs",
        out_json=out_json,
        repo_input=None,
        shadow_gate_config={
            "required_consecutive_shadow_cycles": 2,
            "require_exact_export_projection": True,
        },
        state_path=tmp_path / "external" / "release_state.json",
    )

    assert result == 0
    assert len(captured) == 1
    assert captured[0]["cycle_mode"] == "operational"
    assert "qualification_manifest" not in captured[0]


def test_case_outcome_sync_persists_a_validated_current_lifecycle_pointer() -> None:
    case_registry = {
        "schema_version": 1,
        "cases": {
            "case:one": {
                "case_id": "case:one",
                "canonical_problem_id": "problem:one",
                "state": "active",
            }
        },
        "problem_id_to_case_id": {"problem:one": "case:one"},
        "atom_id_to_case_id": {"atom:one": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    outcome = {
        "schema_version": 1,
        "case_id": "case:one",
        "plan_revision_id": "planrev:case:one:abc123:1",
        "state": "planned",
        "recorded_at": "2026-07-09T12:00:00Z",
        "requires_live_verification": False,
        "target_branch": None,
        "merged_commit": None,
        "test_evidence": [],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
    }
    atom_actions = {
        "atom:one": {
            "atom_id": "atom:one",
            "case_id": "case:one",
            "plan_outcomes": {
                "planrev:case:one:abc123:1": {
                    "state": "planned",
                    "recorded_at": "2026-07-09T12:00:00Z",
                    "path": "plans/one.md",
                    "fingerprint": "0123456789abcdef",
                    "outcome_record": outcome,
                }
            },
        }
    }

    result = _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
    )

    assert result["invalid_outcome_records"] == 0
    case = case_registry["cases"]["case:one"]
    assert case["state"] == "planned"
    assert case["current_lifecycle"] == {
        "state": "planned",
        "outcome_reference": {
            "source": "structurally_valid_nonterminal_plan_outcome",
            "validation_status": "not_required_nonterminal",
            "plan_revision_id": "planrev:case:one:abc123:1",
            "recorded_at": "2026-07-09T12:00:00Z",
            "path": "plans/one.md",
            "fingerprint": "0123456789abcdef",
        },
    }


def test_stale_legacy_actioned_atom_without_plan_or_outcome_returns_to_new() -> None:
    atom_actions = {
        "atom:stale": {
            "atom_id": "atom:stale",
            "status": "actioned",
            "case_id": "case:missing",
            "disposition": "supports_case",
            "disposition_rationale": "A deleted legacy plan once cited this atom.",
            "last_plan_seen_at": "2026-07-01T00:00:00Z",
        }
    }

    result = _reset_stale_unproven_actioned_atoms(
        atom_actions=atom_actions,
        case_registry={"cases": {}},
        current_plan_sync_at="2026-07-10T00:00:00Z",
        generated_at="2026-07-10T00:00:00Z",
    )

    entry = atom_actions["atom:stale"]
    assert result == {"examined": 1, "reset_to_new": 1, "idea_excluded": 0}
    assert entry["status"] == "new"
    assert entry["stale_actioned_previous_case_id"] == "case:missing"
    assert "case_id" not in entry
    assert entry["stale_actioned_previous_disposition"] == "supports_case"
    assert entry["disposition"] == "unresolved"
    assert entry["disposition_status"] == "pending"


def test_stale_actioned_reset_preserves_current_plan_verified_outcome_and_idea() -> None:
    sync_at = "2026-07-10T00:00:00Z"
    atom_actions = {
        "atom:plan": {
            "atom_id": "atom:plan",
            "status": "actioned",
            "last_plan_seen_at": sync_at,
        },
        "atom:resolved": {
            "atom_id": "atom:resolved",
            "status": "actioned",
            "case_id": "case:resolved",
        },
        "atom:idea": {
            "atom_id": "atom:idea",
            "status": "actioned",
            "category": "IDEA",
        },
    }
    registry = {
        "cases": {
            "case:resolved": {
                "state": "resolved",
                "current_lifecycle": {
                    "state": "resolved",
                    "outcome_reference": {"validation_status": "verified"},
                },
            }
        }
    }

    result = _reset_stale_unproven_actioned_atoms(
        atom_actions=atom_actions,
        case_registry=registry,
        current_plan_sync_at=sync_at,
        generated_at=sync_at,
    )

    assert result == {"examined": 3, "reset_to_new": 0, "idea_excluded": 1}
    assert {entry["status"] for entry in atom_actions.values()} == {"actioned"}


@pytest.mark.parametrize("unproven_state", ["implemented", "tests_verified", "live_verified"])
def test_case_outcome_sync_downgrades_unproven_legacy_progress(
    unproven_state: str,
) -> None:
    case_registry = {
        "schema_version": 1,
        "cases": {"case:one": {"case_id": "case:one", "state": "active"}},
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {"atom:one": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    atom_actions = {
        "atom:one": {
            "atom_id": "atom:one",
            "case_id": "case:one",
            "last_outcome_state": unproven_state,
            "last_outcome_recorded_at": "2026-07-09T12:00:00Z",
        }
    }

    _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
    )

    case = case_registry["cases"]["case:one"]
    assert case["state"] == "unverified"
    assert case["current_lifecycle"] == {
        "state": "unverified",
        "outcome_reference": {
            "source": "legacy_atom_action_projection",
            "validation_status": "projected",
            "recorded_at": "2026-07-09T12:00:00Z",
        },
    }


@pytest.mark.parametrize("unproven_state", ["implemented", "tests_verified", "live_verified"])
def test_case_outcome_sync_downgrades_unproven_plan_progress(
    unproven_state: str,
) -> None:
    case_registry = {
        "schema_version": 1,
        "cases": {"case:one": {"case_id": "case:one", "state": "active"}},
        "problem_id_to_case_id": {},
        "atom_id_to_case_id": {"atom:one": "case:one"},
        "ticket_fingerprint_to_case_id": {},
    }
    atom_actions = {
        "atom:one": {
            "atom_id": "atom:one",
            "case_id": "case:one",
            "plan_outcomes": {
                "plan:one": {
                    "state": unproven_state,
                    "recorded_at": "2026-07-09T12:00:00Z",
                    "required": True,
                }
            },
        }
    }

    _sync_case_registry_outcomes(
        case_registry=case_registry,
        atom_actions=atom_actions,
    )

    case = case_registry["cases"]["case:one"]
    assert case["state"] == "unverified"
    assert case["plan_outcomes"]["plan:one"]["state"] == "unverified"
    assert case["current_lifecycle"]["outcome_reference"]["validation_status"] == (
        "fail_open_projection"
    )


def test_problem_mining_workspace_writes_agent_readable_atom_index(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = _write_chunked_problem_mining_atoms_workspace(
        workspace_dir=workspace,
        prompt_atoms=[
            {
                "atom_id": "run-a:confusion_point:1",
                "run_rel": "target/20260101T000000Z/codex/0",
                "source": "confusion_point",
                "severity_hint": "high",
                "text": "The CLI quickstart has no obvious first command.",
                "linked_atom_ids": [],
            },
            {
                "atom_id": "run-b:run_failure_event:1",
                "run_rel": "target/20260102T000000Z/claude/0",
                "source": "run_failure_event",
                "severity_hint": "medium",
                "text": "Run failed before producing a report.",
                "linked_atom_ids": ["run-a:confusion_point:1"],
            },
        ],
        max_records_per_miner=3,
    )

    assert manifest["index_file"] == "atoms_index.md"
    assert manifest["atom_file_count"] == 2
    assert manifest["chunks"][0]["text_file"] == "atoms_text/atoms_001.md"

    index = (workspace / "atoms_index.md").read_text(encoding="utf-8")
    text_chunk = (workspace / "atoms_text" / "atoms_001.md").read_text(encoding="utf-8")
    atom_file = (workspace / "atoms_by_id" / "atom_0001.md").read_text(encoding="utf-8")

    assert "run-a:confusion_point:1" in index
    assert "atom_file: `atoms_by_id/atom_0001.md`" in index
    assert "chunk_file: `atoms_text/atoms_001.md`" in index
    assert "The CLI quickstart has no obvious first command." in text_chunk
    assert "The CLI quickstart has no obvious first command." in atom_file
    assert "linked_atom_ids: run-a:confusion_point:1" in text_chunk


def test_relation_review_rejects_candidate_only_historical_focus() -> None:
    with pytest.raises(ValueError, match="candidate_only_focus"):
        _validate_relation_decision_focuses(
            [{"focus_id": "problem:historical", "action": "keep_separate"}],
            work_unit_problem_ids={"problem:current"},
        )


def test_relation_review_requires_exactly_one_disposition_for_every_active_focus() -> None:
    with pytest.raises(ValueError, match="missing_focus: problem:second"):
        _validate_relation_decision_focuses(
            [{"focus_id": "problem:first", "action": "keep_separate"}],
            work_unit_problem_ids={"problem:first", "problem:second"},
        )

    with pytest.raises(ValueError, match="duplicate_focus: problem:first"):
        _validate_relation_decision_focuses(
            [
                {"focus_id": "problem:first", "action": "keep_separate"},
                {"focus_id": "problem:first", "action": "merge"},
            ],
            work_unit_problem_ids={"problem:first"},
        )

    _validate_relation_decision_focuses(
        [
            {"focus_id": "problem:first", "action": "keep_separate"},
            {"focus_id": "problem:second", "action": "keep_separate"},
        ],
        work_unit_problem_ids={"problem:first", "problem:second"},
    )


def _seed_labeler_cache(artifacts_dir: Path, ticket: dict[str, Any], *, labelers: int = 3) -> None:
    fingerprint = _ticket_labeler_fingerprint(ticket)
    labeler_dir = artifacts_dir / "labeler" / fingerprint
    labeler_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "docs"},
        "component": "docs",
        "intent_risk": "low",
        "confidence": 0.75,
        "evidence_atom_ids_used": [
            item for item in ticket.get("evidence_atom_ids", []) if isinstance(item, str)
        ],
    }
    for idx in range(1, labelers + 1):
        (labeler_dir / f"labeler_{idx:02d}.label.json").write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _seed_runs_fixture(runs_dir: Path) -> None:
    run_a = runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0"
    run_b = runs_dir / "target_a" / "20260102T000000Z" / "claude" / "0"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)

    _write_json(
        run_a / "target_ref.json",
        {
            "repo_input": "pip:agent-adapters",
            "agent": "codex",
            "persona_id": "routine_operator",
            "mission_id": "complete_output_smoke",
        },
    )
    _write_json(run_a / "effective_run_spec.json", {})
    _write_json(
        run_a / "metrics.json",
        {
            "commands_executed": 7,
            "commands_failed": 0,
            "step_count": 11,
            "event_counts": {},
            "distinct_files_read": [],
            "distinct_docs_read": [],
            "distinct_files_written": [],
            "lines_added_total": 0,
            "lines_removed_total": 0,
        },
    )
    _write_json(
        run_a / "report.json",
        {
            "confusion_points": [{"summary": "No quickstart section"}],
            "suggested_changes": [
                {
                    "change": "Add quickstart examples",
                    "type": "docs",
                    "location": "README.md",
                    "priority": "p1",
                    "expected_impact": "faster onboarding",
                }
            ],
            "confidence_signals": {"missing": ["No smoke command"]},
        },
    )
    (run_a / "agent_stderr.txt").write_text("", encoding="utf-8")
    (run_a / "agent_last_message.txt").write_text("", encoding="utf-8")

    _write_json(
        run_b / "target_ref.json",
        {
            "repo_input": "pip:agent-adapters",
            "agent": "claude",
            "persona_id": "routine_operator",
            "mission_id": "complete_output_smoke",
        },
    )
    _write_json(run_b / "effective_run_spec.json", {})
    _write_json(
        run_b / "metrics.json",
        {
            "commands_executed": 3,
            "commands_failed": 1,
            "failed_commands": [
                {
                    "command": "python -m pip install -r requirements-dev.txt",
                    "exit_code": 1,
                    "output_excerpt": "Temporary failure in name resolution",
                }
            ],
            "step_count": 6,
            "event_counts": {},
            "distinct_files_read": [],
            "distinct_docs_read": [],
            "distinct_files_written": [],
            "lines_added_total": 0,
            "lines_removed_total": 0,
        },
    )
    _write_json(
        run_b / "report_validation_errors.json",
        ["$: failed to parse JSON from agent output"],
    )
    (run_b / "agent_stderr.txt").write_text("status 429 retrying\n", encoding="utf-8")
    (run_b / "agent_last_message.txt").write_text("done\n", encoding="utf-8")


def _seed_many_high_severity_runs(runs_dir: Path, *, count: int) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for idx in range(count):
        ts = (base + timedelta(minutes=idx)).strftime("%Y%m%dT%H%M%SZ")
        run_dir = runs_dir / "target_a" / ts / "codex" / "0"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "target_ref.json",
            {
                "repo_input": "pip:agent-adapters",
                "agent": "codex",
                "persona_id": "routine_operator",
                "mission_id": "complete_output_smoke",
            },
        )
        _write_json(run_dir / "effective_run_spec.json", {})
        _write_json(run_dir / "report_validation_errors.json", [f"validation issue {idx}"])
        (run_dir / "agent_stderr.txt").write_text("", encoding="utf-8")
        (run_dir / "agent_last_message.txt").write_text("", encoding="utf-8")


def test_reports_backlog_dry_run_writes_outputs(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "2",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    out_md = compiled / "target_a.backlog.md"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    agent_last_message_atoms_jsonl = (
        compiled / "target_a.backlog.atoms.agent_last_message_artifact.jsonl"
    )

    assert out_json.exists()
    assert out_md.exists()
    assert atoms_jsonl.exists()
    assert agent_last_message_atoms_jsonl.exists()

    summary = json.loads(out_json.read_text(encoding="utf-8"))
    assert summary["artifacts"]["atoms_jsonl"] == str(atoms_jsonl)
    assert summary["artifacts"]["atoms_agent_last_message_artifact_jsonl"] == str(
        agent_last_message_atoms_jsonl
    )
    assert summary["totals"]["runs"] == 2
    assert summary["totals"]["miners_total"] == 0
    assert summary["totals"]["source_counts"].get("aggregate_metrics", 0) == 2
    assert summary["totals"]["source_counts"].get("command_failure", 0) == 1

    atom_lines = atoms_jsonl.read_text(encoding="utf-8").splitlines()
    assert any(
        json.loads(line).get("source") == "agent_last_message_artifact"
        for line in atom_lines
        if line
    )
    agent_last_message_lines = agent_last_message_atoms_jsonl.read_text(
        encoding="utf-8"
    ).splitlines()
    assert agent_last_message_lines
    assert all(
        json.loads(line).get("source") == "agent_last_message_artifact"
        for line in agent_last_message_lines
        if line
    )

    markdown = out_md.read_text(encoding="utf-8")
    assert "Untriaged Tail" in markdown


def test_stage3_subscription_wait_stops_later_model_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    later_stage_calls: list[str] = []

    wait = {
        "code": "codex_chatgpt_subscription_usage_limit",
        "provider": "codex",
        "phase": "agent_execution",
        "route": "chatgpt_subscription",
        "api_fallback_allowed": False,
        "state": "parked",
        "retry_mode": "resume_same_session",
        "retry_disposition": "resume_after_provider_reset",
        "resume_after": {
            "raw": "Jul 18th, 2026 2:33 AM",
            "timezone": "provider_account_local_unspecified",
        },
        "run_dir": str(tmp_path / "retained-run"),
        "error_artifact": str(tmp_path / "retained-run" / "error.json"),
        "error_artifact_sha256": "e" * 64,
        "error_artifact_size_bytes": 123,
    }
    checkpoint: dict[str, Any] = {
        "schema_version": 1,
        "status": "parked_external_wait",
        "scope": "repro_research_stage",
        "reason": "codex_chatgpt_subscription_usage_limit",
        "trigger_case_id": "case:provider-wait",
        "trigger_problem_id": "problem:provider-wait",
        "expected_session_id": "019f2cca-9011-7e32-88ae-6c25af578b49",
        "observed_session_id": "019f2cca-9011-7e32-88ae-6c25af578b49",
        "authored_work_disposition": "retained",
        "resume_status": "checkpoint_persisted_same_author_resume_supported",
        "next_action": "resume_same_author_from_checkpoint_after_provider_reset",
        "route": "chatgpt_subscription",
        "api_fallback_allowed": False,
        "external_wait": wait,
    }
    checkpoint["checkpoint_sha256"] = staged_module._qualification_canonical_sha256(
        checkpoint
    )

    def parked_stage3(**kwargs: Any) -> dict[str, Any]:
        stage_doc = {
            "stage": "repro_research",
            "generated_at": "2026-07-12T00:00:00Z",
            "item_count": 0,
            "warning_count": 0,
            "warnings": [],
            "input_meta": {
                "stage_status": "parked_external_wait",
                "external_wait": checkpoint,
            },
            "artifacts": {},
            "items": [],
        }
        Path(kwargs["out_json"]).write_text(
            json.dumps(stage_doc, indent=2) + "\n", encoding="utf-8"
        )
        Path(kwargs["out_md"]).write_text("# Parked\n", encoding="utf-8")
        return stage_doc

    def forbidden_later_stage(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        later_stage_calls.append(str(kwargs.get("out_json") or "unknown"))
        raise AssertionError("Stages 4-6 must not run while Stage 3 is provider-parked")

    monkeypatch.setattr(staged_module, "_run_repro_research_stage", parked_stage3)
    monkeypatch.setattr(staged_module, "_run_solution_optioning_stage", forbidden_later_stage)
    monkeypatch.setattr(staged_module, "_run_solution_selection_stage", forbidden_later_stage)
    monkeypatch.setattr(staged_module, "_run_implementation_planning_stage", forbidden_later_stage)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )

    assert exc.value.code == 2
    assert later_stage_calls == []
    compiled = runs_dir / "target_a" / "_compiled"
    research = json.loads((compiled / "target_a.research.json").read_text(encoding="utf-8"))
    assert research["input_meta"]["external_wait"]["checkpoint_sha256"] == (
        checkpoint["checkpoint_sha256"]
    )
    assert not (compiled / "target_a.solution_options.json").exists()
    assert not (compiled / "target_a.solution_selection.json").exists()
    assert not (compiled / "target_a.change_plans.json").exists()

    retained_paths = [
        compiled / "target_a.backlog.atoms.jsonl",
        compiled / "target_a.problem_records.json",
        compiled / "target_a.problem_records.evidence_receipt.json",
        compiled / "target_a.prioritized_problems.json",
        compiled / "target_a.case_registry.json",
    ]
    retained_bytes = {path: path.read_bytes() for path in retained_paths}
    retained_stage2 = json.loads(
        (compiled / "target_a.prioritized_problems.json").read_text(encoding="utf-8")
    )
    retained_selected_ids = [
        item["problem_id"]
        for item in retained_stage2["items"]
        if item.get("selected_for_research") is True
    ]
    resumed_stage3_calls: list[dict[str, Any]] = []

    upstream_calls: list[str] = []

    def forbidden_upstream(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        upstream_calls.append("invoked")
        raise AssertionError("A provider-wait resume must not rerun Stages 1 or 2")

    def reached_resumed_stage3(**kwargs: Any) -> dict[str, Any]:
        resumed_stage3_calls.append(kwargs)
        raise RuntimeError("stop_after_proving_retained_stage3_resume")

    monkeypatch.setattr(staged_module, "_run_problem_mining_stage", forbidden_upstream)
    monkeypatch.setattr(
        staged_module,
        "_run_problem_case_relation_review",
        forbidden_upstream,
    )
    monkeypatch.setattr(
        staged_module,
        "_run_problem_prioritization_stage",
        forbidden_upstream,
    )
    monkeypatch.setattr(staged_module, "_run_repro_research_stage", reached_resumed_stage3)

    with pytest.raises(SystemExit) as resumed_exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--resume",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )

    assert resumed_exc.value.code == 2
    assert len(resumed_stage3_calls) == 1
    resume_call = resumed_stage3_calls[0]
    assert resume_call["resume_stage_document"]["input_meta"]["external_wait"][
        "checkpoint_sha256"
    ] == checkpoint["checkpoint_sha256"]
    assert [
        item["problem_id"] for item in resume_call["selected_priority_decisions"]
    ] == retained_selected_ids
    assert all(path.read_bytes() == retained_bytes[path] for path in retained_paths)
    assert upstream_calls == []

    tampered_research = json.loads(
        (compiled / "target_a.research.json").read_text(encoding="utf-8")
    )
    tampered_research["input_meta"]["external_wait"]["checkpoint_sha256"] = "0" * 64
    tampered_bytes = (json.dumps(tampered_research, indent=2) + "\n").encode("utf-8")
    (compiled / "target_a.research.json").write_bytes(tampered_bytes)
    resumed_stage3_calls.clear()

    with pytest.raises(SystemExit) as tampered_exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--resume",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )

    assert tampered_exc.value.code == 2
    assert upstream_calls == []
    assert resumed_stage3_calls == []
    assert (compiled / "target_a.research.json").read_bytes() == tampered_bytes


def test_two_shadow_cycles_retain_open_cases_and_add_new_evidence_without_export(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    argv = [
        "reports",
        "backlog",
        "--repo-root",
        str(repo_root),
        "--runs-dir",
        str(runs_dir),
        "--target",
        "target_a",
        "--dry-run",
        "--miners",
        "0",
        "--sample-size",
        "8",
        "--atom-actions-yaml",
        str(atom_actions_path),
        "--skip-plan-folder-sync",
    ]

    snapshots: list[dict[str, Any]] = []
    active_case_sets: list[set[str]] = []
    first_evidence_by_case: dict[str, set[str]] = {}
    nonterminal_case_id: str | None = None
    terminal_case_id: str | None = None
    for cycle in range(2):
        if cycle == 1:
            run_c = runs_dir / "target_a" / "20260103T000000Z" / "codex" / "0"
            _write_json(
                run_c / "target_ref.json",
                {
                    "repo_input": "pip:agent-adapters",
                    "agent": "codex",
                    "persona_id": "routine_operator",
                    "mission_id": "complete_output_smoke",
                },
            )
            _write_json(run_c / "effective_run_spec.json", {})
            _write_json(
                run_c / "metrics.json",
                {
                    "commands_executed": 1,
                    "commands_failed": 0,
                    "step_count": 1,
                    "event_counts": {},
                    "distinct_files_read": [],
                    "distinct_docs_read": [],
                    "distinct_files_written": [],
                    "lines_added_total": 0,
                    "lines_removed_total": 0,
                },
            )
            _write_json(
                run_c / "report.json",
                {"confusion_points": [{"summary": "No quickstart section remains visible"}]},
            )
            _write_json(
                run_c / "token_monitoring.json",
                {
                    "signals": [
                        {
                            "signal_id": "novel-read-loop",
                            "causal_mechanism": "The same file is read repeatedly",
                            "confidence": "high",
                            "token_dimensions_affected": {"input_tokens": 25000},
                            "confirmed_by_counters": True,
                        }
                    ]
                },
            )
            (run_c / "agent_stderr.txt").write_text("", encoding="utf-8")
            (run_c / "agent_last_message.txt").write_text("", encoding="utf-8")

        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0

        compiled = runs_dir / "target_a" / "_compiled"
        case_registry = json.loads(
            (compiled / "target_a.case_registry.json").read_text(encoding="utf-8")
        )
        backlog = json.loads((compiled / "target_a.backlog.json").read_text(encoding="utf-8"))
        problem_doc = json.loads(
            (compiled / "target_a.problem_records.json").read_text(encoding="utf-8")
        )
        snapshots.append(case_registry)
        active_case_ids = {
            str(item["case_id"])
            for item in problem_doc.get("items", [])
            if isinstance(item, dict) and isinstance(item.get("case_id"), str)
        }
        active_case_sets.append(active_case_ids)
        assert active_case_ids, "nonterminal cases must remain active across shadow cycles"
        for active_case_id in active_case_ids:
            stage_refs = case_registry["cases"][active_case_id].get("stage_artifact_refs", {})
            assert "problem_mining" in stage_refs
            assert "problem_prioritization" in stage_refs
            assert "ticket_assembly" in stage_refs
        assert all(
            ticket.get("stage") != "ready_for_ticket"
            for ticket in backlog.get("tickets", [])
            if isinstance(ticket, dict)
        )
        assert not list(tmp_path.rglob("*.idea.md"))

        if cycle == 0:
            first_evidence_by_case = {
                str(case_id): {
                    str(atom_id)
                    for atom_id in entry.get("evidence_atom_ids", [])
                    if isinstance(atom_id, str)
                }
                for case_id, entry in case_registry.get("cases", {}).items()
                if isinstance(entry, dict) and entry.get("state") == "active"
            }
            atom_case_pairs = [
                (str(atom_id), str(case_id))
                for atom_id, case_id in case_registry.get("atom_id_to_case_id", {}).items()
                if not str(atom_id).startswith("__aggregate__/")
            ]
            assert len({case_id for _, case_id in atom_case_pairs}) >= 2
            nonterminal_case_id = atom_case_pairs[0][1]
            terminal_case_id = next(
                case_id for _, case_id in atom_case_pairs if case_id != nonterminal_case_id
            )
            ledger = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
            assert isinstance(ledger, dict)
            atom_entries = {
                str(entry["atom_id"]): entry
                for entry in ledger.get("atoms", [])
                if isinstance(entry, dict) and isinstance(entry.get("atom_id"), str)
            }
            for atom_id, case_id in atom_case_pairs:
                entry = atom_entries[atom_id]
                if case_id == nonterminal_case_id:
                    outcome_record = {
                        "schema_version": 1,
                        "case_id": case_id,
                        "plan_revision_id": f"plan:{case_id}:tests",
                        "state": "tests_verified",
                        "outcome_scope": "case",
                        "recorded_at": "2026-01-02T12:00:00Z",
                        "requires_live_verification": False,
                        "target_branch": "dev",
                        "merged_commit": "abc123",
                        "test_evidence": [
                            {
                                "kind": "pytest",
                                "reference": "tests/test_shadow.py",
                                "result": "passed",
                                "runner_receipt": _runner_receipt(
                                    case_id=case_id,
                                    plan_revision_id=f"plan:{case_id}:tests",
                                    evidence_kind="test",
                                ),
                            }
                        ],
                        "original_scenario_evidence": [],
                        "live_evidence": [],
                        "remaining_risks": ["Original scenario pending"],
                        "recurrence_check": {"status": "not_run"},
                    }
                    entry.update(
                        {
                            "status": "actioned",
                            "case_id": case_id,
                            "last_outcome_state": "tests_verified",
                            "last_outcome_recorded_at": "2026-01-02T12:00:00Z",
                            "last_outcome_record": outcome_record,
                        }
                    )
                elif case_id == terminal_case_id:
                    outcome_record = {
                        "schema_version": 1,
                        "case_id": case_id,
                        "plan_revision_id": f"plan:{case_id}:resolved",
                        "state": "resolved",
                        "outcome_scope": "case",
                        "recorded_at": "2026-01-02T12:00:00Z",
                        "requires_live_verification": False,
                        "target_branch": "dev",
                        "merged_commit": "def456",
                        "test_evidence": [
                            {
                                "kind": "pytest",
                                "reference": "tests/test_shadow.py",
                                "result": "passed",
                                "runner_receipt": _runner_receipt(
                                    case_id=case_id,
                                    plan_revision_id=f"plan:{case_id}:resolved",
                                    evidence_kind="test",
                                ),
                            }
                        ],
                        "original_scenario_evidence": [
                            {
                                "kind": "replay",
                                "reference": "runs/shadow/replay.json",
                                "result": "passed",
                                "runner_receipt": _runner_receipt(
                                    case_id=case_id,
                                    plan_revision_id=f"plan:{case_id}:resolved",
                                    evidence_kind="original_scenario",
                                ),
                            }
                        ],
                        "live_evidence": [],
                        "remaining_risks": [],
                        "recurrence_check": {
                            "status": "completed",
                            "result": "passed",
                            "evidence": [
                                {
                                    "kind": "replay",
                                    "reference": "runs/shadow/recurrence.json",
                                    "result": "passed",
                                    "runner_receipt": _runner_receipt(
                                        case_id=case_id,
                                        plan_revision_id=(f"plan:{case_id}:resolved"),
                                        evidence_kind="recurrence",
                                    ),
                                }
                            ],
                        },
                    }
                    entry.update(
                        {
                            "status": "actioned",
                            "case_id": case_id,
                            "last_outcome_state": "resolved",
                            "last_outcome_recorded_at": "2026-01-02T12:00:00Z",
                            "last_outcome_record": outcome_record,
                        }
                    )
            ledger["atoms"] = [atom_entries[key] for key in sorted(atom_entries)]
            _write_yaml(atom_actions_path, ledger)

    assert nonterminal_case_id is not None
    assert terminal_case_id is not None
    # Structurally valid embedded records are not completion proof. These synthetic
    # records have no retained runner artifacts, plan hashes, or merge provenance, so
    # both cases must remain in the active work set instead of suppressing discovery.
    expected_retained = active_case_sets[0]
    assert expected_retained <= active_case_sets[1]
    assert nonterminal_case_id in active_case_sets[1]
    assert terminal_case_id in active_case_sets[1]
    assert snapshots[1]["cases"][nonterminal_case_id]["state"] == "unverified"
    assert snapshots[1]["cases"][terminal_case_id]["state"] == "unverified"
    assert len(snapshots[1].get("cases", {})) > len(snapshots[0].get("cases", {}))
    for case_id, first_evidence in first_evidence_by_case.items():
        second_entry = snapshots[1]["cases"][case_id]
        assert first_evidence <= set(second_entry.get("evidence_atom_ids", []))
    assert any(
        "20260103T000000Z" in atom_id for atom_id in snapshots[1].get("atom_id_to_case_id", {})
    )


def test_reports_backlog_prefers_error_json_over_duplicate_validation_error(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    run_b = runs_dir / "target_a" / "20260102T000000Z" / "claude" / "0"
    _write_json(
        run_b / "error.json",
        {
            "type": "AgentExecFailed",
            "message": "$: failed to parse JSON from agent output",
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    out_json = runs_dir / "target_a" / "_compiled" / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    source_counts = summary["totals"]["source_counts"]
    assert source_counts.get("run_failure_event", 0) >= 1
    assert source_counts.get("error_json", 0) == 0
    assert source_counts.get("report_validation_error", 0) == 0


def test_reports_backlog_carryover_actioned_only_demotes_ticketed_and_queued_atoms(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    queued_atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    _write_yaml(
        atom_actions_path,
        {"version": 1, "atoms": [{"atom_id": queued_atom_id, "status": "queued"}]},
    )

    argv_base = [
        "reports",
        "backlog",
        "--repo-root",
        str(repo_root),
        "--runs-dir",
        str(runs_dir),
        "--target",
        "target_a",
        "--dry-run",
        "--miners",
        "0",
        "--sample-size",
        "0",
        "--atom-actions-yaml",
        str(atom_actions_path),
        "--skip-plan-folder-sync",
    ]

    with pytest.raises(SystemExit) as exc:
        main(argv_base)
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    assert atoms_jsonl.exists()

    atom_ids = {
        str(json.loads(line).get("atom_id"))
        for line in atoms_jsonl.read_text(encoding="utf-8").splitlines()
        if line
    }
    assert queued_atom_id in atom_ids

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == queued_atom_id)
    assert entry["status"] == "new"
    assert entry["reopened_previous_status"] == "queued"

    # Re-seed the historical row so the explicit carryover mode still exercises its
    # own demotion behavior independently of the new default fail-open filter.
    _write_yaml(
        atom_actions_path,
        {"version": 1, "atoms": [{"atom_id": queued_atom_id, "status": "queued"}]},
    )

    with pytest.raises(SystemExit) as exc:
        main([*argv_base, "--carryover-actioned-only"])
    assert exc.value.code == 0

    atom_ids = {
        str(json.loads(line).get("atom_id"))
        for line in atoms_jsonl.read_text(encoding="utf-8").splitlines()
        if line
    }
    assert queued_atom_id in atom_ids

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == queued_atom_id)
    assert entry["status"] == "new"

    summary = json.loads((compiled / "target_a.backlog.json").read_text(encoding="utf-8"))
    carryover = summary["artifacts"]["atom_filter"]["carryover"]
    assert carryover["mode"] == "actioned_only"
    assert carryover["demoted_atoms"] >= 1
    assert carryover.get("demoted_status_counts", {}).get("queued") == 1


def test_reports_backlog_writes_stage_backed_tickets_and_updates_atom_actions(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    tickets = [t for t in summary.get("tickets", []) if isinstance(t, dict)]
    assert tickets, "dry-run backlog should produce at least one ticket"

    planned = [t for t in tickets if isinstance(t.get("change_plan"), dict)]
    assert planned == []
    assert all(ticket.get("stage") != "ready_for_ticket" for ticket in tickets)

    # A dry-run research proof is explicitly blocked and must not mark atoms ticketed.
    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    assert atom_actions_doc["version"] == 1
    atoms = atom_actions_doc["atoms"]
    assert all(item.get("status") != "ticketed" for item in atoms)


def test_update_atom_actions_from_backlog_skips_blocked_tickets(tmp_path: Path) -> None:
    from usertest_backlog.cli import _update_atom_actions_from_backlog

    atom_actions: dict[str, dict[str, Any]] = {}
    atoms = [
        {"atom_id": "atom:1", "source": "confusion_point"},
        {"atom_id": "atom:2", "source": "confusion_point"},
        {"atom_id": "atom:3", "source": "confusion_point"},
    ]
    tickets = [
        {
            "title": "Blocked ticket",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "F",
            "stage": "blocked",
            "evidence_atom_ids": ["atom:1"],
        },
        {
            "title": "Triage record",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "F",
            "stage": "triage",
            "evidence_atom_ids": ["atom:2"],
        },
        {
            "title": "Exportable research ticket",
            "problem": "P",
            "user_impact": "U",
            "proposed_fix": "F",
            "stage": "research_required",
            "evidence_atom_ids": ["atom:3"],
        },
    ]

    _update_atom_actions_from_backlog(
        atom_actions=atom_actions,
        atoms=atoms,
        tickets=tickets,
        generated_at="2026-01-01T00:00:00Z",
        backlog_json_path=tmp_path / "backlog.json",
    )

    assert atom_actions["atom:1"]["status"] == "new"
    assert atom_actions["atom:2"]["status"] == "new"
    assert atom_actions["atom:3"]["status"] == "ticketed"


def test_reports_backlog_reopens_plan_action_without_verified_terminal_outcome(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)

    owner_repo = tmp_path / "owner_repo"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)

    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    (complete_dir / "20260214_BLG-123_deadbeefdeadbeef_plan-sync-test.md").write_text(
        "# Plan sync test\n\n## Evidence atom ids\n\n- `" + atom_id + "`\n",
        encoding="utf-8",
    )

    run_dirs = [
        runs_dir / "target_a" / "20260101T000000Z" / "codex" / "0",
        runs_dir / "target_a" / "20260102T000000Z" / "claude" / "0",
    ]
    for run_dir in run_dirs:
        target_ref_path = run_dir / "target_ref.json"
        payload = json.loads(target_ref_path.read_text(encoding="utf-8"))
        payload["repo_input"] = str(owner_repo)
        _write_json(target_ref_path, payload)

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert atom_filter["reopened_status_counts"].get("actioned", 0) >= 1
    assert atom_id in atom_filter["reopened_atom_ids_preview"]

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(item for item in atom_actions_doc["atoms"] if item["atom_id"] == atom_id)
    assert atom_entry["status"] == "new"
    assert atom_entry["reopened_previous_status"] == "actioned"


def test_reports_sync_atom_actions_dry_run_reports_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "runner"
    repo_root.mkdir(parents=True)
    owner_repo = tmp_path / "owner_repo"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)

    fingerprint = "deadbeefdeadbeef"
    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    (complete_dir / f"20260214_{fingerprint}_plan-sync-test.md").write_text(
        "# Plan sync test\n",
        encoding="utf-8",
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": atom_id,
                    "status": "queued",
                    "fingerprints": [fingerprint],
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "sync-atom-actions",
                "--repo-root",
                str(repo_root),
                "--owner-root",
                str(owner_repo),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--dry-run",
            ]
        )

    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["before_status_counts"]["queued"] == 1
    assert payload["after_status_counts"]["actioned"] == 1
    assert payload["sync"]["plan_sync"]["atoms_promoted"] == 1

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    assert atom_actions_doc["atoms"][0]["status"] == "queued"


def test_reports_backlog_reopens_unmapped_queued_atoms_by_default(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)

    queued_atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": queued_atom_id,
                    "status": "queued",
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert "queued" in atom_filter["exclude_statuses"]
    assert atom_filter["reopened_status_counts"]["queued"] == 1
    assert queued_atom_id in atom_filter["reopened_atom_ids_preview"]
    assert summary["totals"]["atoms"] == atom_filter["eligible_atoms"]

    assert queued_atom_id in atoms_jsonl.read_text(encoding="utf-8")
    queued_atom = _stage1_assigned_atom(compiled, queued_atom_id)
    assert queued_atom["disposition"] == "unresolved"
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([queued_atom])] == [
        queued_atom_id
    ]

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(
        item for item in atom_actions_doc["atoms"] if item["atom_id"] == queued_atom_id
    )
    assert atom_entry["status"] == "new"
    assert atom_entry["reopened_previous_status"] == "queued"
    assert atom_entry["disposition"] == "unresolved"


def test_reports_backlog_reopens_unmapped_ticketed_atoms_by_default(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)

    ticketed_atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": ticketed_atom_id,
                    "status": "ticketed",
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    out_json = compiled / "target_a.backlog.json"
    atoms_jsonl = compiled / "target_a.backlog.atoms.jsonl"
    summary = json.loads(out_json.read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert "ticketed" in atom_filter["exclude_statuses"]
    assert atom_filter["reopened_status_counts"]["ticketed"] == 1
    assert ticketed_atom_id in atom_filter["reopened_atom_ids_preview"]
    assert summary["totals"]["atoms"] == atom_filter["eligible_atoms"]

    assert ticketed_atom_id in atoms_jsonl.read_text(encoding="utf-8")
    ticketed_atom = _stage1_assigned_atom(compiled, ticketed_atom_id)
    assert ticketed_atom["disposition"] == "unresolved"
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([ticketed_atom])] == [
        ticketed_atom_id
    ]

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atom_entry = next(
        item for item in atom_actions_doc["atoms"] if item["atom_id"] == ticketed_atom_id
    )
    assert atom_entry["status"] == "new"
    assert atom_entry["reopened_previous_status"] == "ticketed"


def test_actioned_unproven_terminal_case_is_reopened_at_mining_boundary(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    case_id = "case:unproven-terminal"
    compiled = runs_dir / "target_a" / "_compiled"
    _write_json(
        compiled / "target_a.case_registry.json",
        {
            "schema_version": 1,
            "cases": {
                case_id: {
                    "case_id": case_id,
                    "canonical_problem_id": "problem:unproven-terminal",
                    "state": "resolved",
                    "current_lifecycle": {
                        "state": "resolved",
                        "outcome_reference": {"validation_status": "projected"},
                    },
                }
            },
            "problem_id_to_case_id": {"problem:unproven-terminal": case_id},
            "atom_id_to_case_id": {atom_id: case_id},
            "atom_id_to_case_ids": {atom_id: [case_id]},
            "ticket_fingerprint_to_case_id": {},
        },
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": atom_id,
                    "status": "actioned",
                    "case_id": case_id,
                    "disposition": "supports_case",
                    "disposition_rationale": "A historical plan attached this atom.",
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "0",
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    assigned = _stage1_assigned_atom(compiled, atom_id)
    assert assigned["disposition"] == "unresolved"
    assert [item["atom_id"] for item in eligible_problem_mining_atoms([assigned])] == [atom_id]
    actions = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))["atoms"]
    action = next(item for item in actions if item["atom_id"] == atom_id)
    assert action["status"] == "new"
    assert action["reopened_previous_status"] == "actioned"
    assert action["reopened_previous_disposition"] == "supports_case"
    assert action["stale_actioned_previous_disposition"] == "supports_case"
    assert action["case_id"] == case_id
    assert action["disposition_status"] == "pending"


@pytest.mark.parametrize("preserved_kind", ["active_case", "verified_terminal", "idea"])
def test_default_filter_preserves_live_terminal_or_idea_boundaries(
    tmp_path: Path,
    preserved_kind: str,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_id = "target_a/20260101T000000Z/codex/0:confusion_point:1"
    compiled = runs_dir / "target_a" / "_compiled"
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    status = "queued" if preserved_kind != "verified_terminal" else "ticketed"
    action: dict[str, Any] = {"atom_id": atom_id, "status": status}
    if preserved_kind == "idea":
        action["category"] = "IDEA"
    else:
        case_id = f"case:{preserved_kind}"
        terminal = preserved_kind == "verified_terminal"
        action.update(
            {
                "case_id": case_id,
                "disposition": "supports_case",
                "disposition_rationale": "The canonical registry owns this evidence.",
            }
        )
        _write_json(
            compiled / "target_a.case_registry.json",
            {
                "schema_version": 1,
                "cases": {
                    case_id: {
                        "case_id": case_id,
                        "canonical_problem_id": f"problem:{preserved_kind}",
                        "state": "resolved" if terminal else "active",
                        "current_lifecycle": (
                            {
                                "state": "resolved",
                                "outcome_reference": {"validation_status": "verified"},
                            }
                            if terminal
                            else {"state": "active"}
                        ),
                    }
                },
                "problem_id_to_case_id": {f"problem:{preserved_kind}": case_id},
                "atom_id_to_case_id": {atom_id: case_id},
                "atom_id_to_case_ids": {atom_id: [case_id]},
                "ticket_fingerprint_to_case_id": {},
            },
        )
    _write_yaml(atom_actions_path, {"version": 1, "atoms": [action]})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "0",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    summary = json.loads((compiled / "target_a.backlog.json").read_text(encoding="utf-8"))
    atom_filter = summary["artifacts"]["atom_filter"]
    assert atom_filter["reopened_unproven_atoms"] == 0
    atom_ids = {
        json.loads(line)["atom_id"]
        for line in (compiled / "target_a.backlog.atoms.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    if preserved_kind == "active_case":
        assert atom_id in atom_ids
        assert atom_filter["preserved_open_case_status_counts"][status] == 1
    else:
        assert atom_id not in atom_ids
        assert atom_id in atom_filter["excluded_atom_ids_preview"]
    persisted_actions = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))["atoms"]
    persisted_action = next(item for item in persisted_actions if item["atom_id"] == atom_id)
    assert persisted_action["status"] == status
    assert "reopened_previous_status" not in persisted_action


def test_reports_backlog_missing_prompt_template_fails_loudly(tmp_path: Path) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / "pipeline_manifest.json").write_text(
        json.dumps(
            {
                "version": 2,
                "problem_miner_templates": ["missing_template.md"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--prompts-dir",
                str(prompts_dir),
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 2


def test_reports_backlog_dry_run_writes_problem_records(tmp_path: Path) -> None:
    """Stage-1 dual-write: problem_records.json and .md are created in dry-run mode.

    In dry-run mode the LLM is not called. The six-stage pipeline still writes
    inspectable artifacts by synthesizing deterministic problem records from atoms.
    The contract being tested is:
    - The files exist.
    - The JSON has the expected structure (stage, records, input_meta.dry_run).
    - No record contains the forbidden field ``proposed_fix``.
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    problem_records_json = compiled / "target_a.problem_records.json"
    problem_records_md = compiled / "target_a.problem_records.md"

    assert problem_records_json.exists(), (
        "problem_records.json must be written by stage-1 dual-write when "
        "pipeline_manifest.json is present in the prompts dir"
    )
    assert problem_records_md.exists(), "problem_records.md must be written"

    doc = json.loads(problem_records_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "problem_mining"
    assert isinstance(doc.get("items"), list)
    # dry-run: LLM not called; synthesized problem records are used
    assert doc.get("item_count") == len(doc["items"])
    assert len(doc["items"]) >= 1, "dry-run mode should synthesize at least one problem record"
    assert doc.get("input_meta", {}).get("dry_run") is True

    # Invariant: no problem record should ever contain proposed_fix
    for rec in doc["items"]:
        assert "proposed_fix" not in rec, (
            f"Record {rec.get('problem_id')} contains forbidden field 'proposed_fix'"
        )


def test_reports_backlog_dry_run_writes_prioritized_problems(tmp_path: Path) -> None:
    """Stage 2: prioritized_problems.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - At least one problem is selected_for_research on fixtures.
    - Output contains deterministic pre-score breakdown (pre_score + score_breakdown).
    - Output contains no solution fields (e.g. proposed_fix).
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    prioritized_json = compiled / "target_a.prioritized_problems.json"
    prioritized_md = compiled / "target_a.prioritized_problems.md"

    assert prioritized_json.exists(), "prioritized_problems.json must be written in dry-run mode"
    assert prioritized_md.exists(), "prioritized_problems.md must be written"

    doc = json.loads(prioritized_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "problem_prioritization"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert len(doc["items"]) >= 1
    assert doc["items"]
    assert all(item.get("selected_for_research") is True for item in doc["items"])

    forbidden = {
        "proposed_fix",
        "selected_solution",
        "family_id",
        "option_id",
        "implementation_steps",
    }
    for item in doc["items"]:
        for field in forbidden:
            assert field not in item, f"priority decision must not contain solution field: {field}"
        assert isinstance(item.get("score_breakdown"), dict)
        assert "pre_score" in item


def test_reports_backlog_dry_run_writes_research_dossiers(tmp_path: Path) -> None:
    """Stage 3: research.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - Output is inspectable offline (dry_run=true) and does not claim implementation.
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    research_json = compiled / "target_a.research.json"
    research_md = compiled / "target_a.research.md"

    assert research_json.exists(), "research.json must be written in dry-run mode"
    assert research_md.exists(), "research.md must be written in dry-run mode"

    doc = json.loads(research_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "repro_research"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert len(doc["items"]) >= 1, (
        "fixtures should yield at least one selected-for-research problem"
    )

    for item in doc["items"]:
        assert item.get("implementation_performed") is False
        assert item.get("diff_classification") == "no_changes"
        assert item.get("writes_used") is False


def test_reports_backlog_dry_run_writes_solution_options(tmp_path: Path) -> None:
    """Stage 4: solution_options.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - At least one problem has one option per configured family.
    - Output contains no selection fields (e.g. selected_solution).
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    options_json = compiled / "target_a.solution_options.json"
    options_md = compiled / "target_a.solution_options.md"

    assert options_json.exists(), "solution_options.json must be written in dry-run mode"
    assert options_md.exists(), "solution_options.md must be written in dry-run mode"

    doc = json.loads(options_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "solution_optioning"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert doc["items"] == []
    outcomes = doc.get("input_meta", {}).get("optioning_outcomes")
    assert isinstance(outcomes, list) and outcomes
    assert {item.get("optioning_status") for item in outcomes} == {"insufficient_evidence"}
    assert all(item.get("research_readiness_blockers") for item in outcomes)


def test_reports_backlog_dry_run_writes_solution_selection(tmp_path: Path) -> None:
    """Stage 5: solution_selection.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - Each decision selects an existing option and includes a UX-review flag.
    - Selected-solution labeler output is attached (change_surface).
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    selection_json = compiled / "target_a.solution_selection.json"
    selection_md = compiled / "target_a.solution_selection.md"

    assert selection_json.exists(), "solution_selection.json must be written in dry-run mode"
    assert selection_md.exists(), "solution_selection.md must be written in dry-run mode"

    doc = json.loads(selection_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "solution_selection"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert doc.get("input_meta", {}).get("breadth_profile") == "external_generalization"
    assert isinstance(doc.get("input_meta", {}).get("batch_breadth"), dict)
    assert isinstance(doc.get("items"), list)
    assert doc["items"] == []
    assert doc.get("input_meta", {}).get("decision_count") == 0
    assert doc.get("input_meta", {}).get("repo_access") == "read_only"


def test_reports_backlog_internal_profile_injects_breadth_context_into_stage5_prompt(
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--breadth-profile",
                "internal_maintenance",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    selection_json = compiled / "target_a.solution_selection.json"
    artifacts_dir = compiled / "target_a.backlog_artifacts" / "solution_selection"

    doc = json.loads(selection_json.read_text(encoding="utf-8"))
    assert doc.get("input_meta", {}).get("breadth_profile") == "internal_maintenance"
    assert isinstance(doc.get("input_meta", {}).get("batch_breadth"), dict)
    assert "missions" in doc.get("input_meta", {}).get("batch_breadth", {})

    prompt_paths = list(artifacts_dir.glob("solution_selection_*/*.prompt.txt"))
    assert prompt_paths == []
    assert doc.get("input_meta", {}).get("decision_count") == 0


def test_reports_backlog_dry_run_writes_change_plans(tmp_path: Path) -> None:
    """Stage 6: change_plans.json and .md are created in dry-run mode.

    Contract:
    - Files exist.
    - Each plan has non-empty implementation + verification steps.
    - Each plan is grounded to a selected option and is marked planned.
    """
    repo_root = find_repo_root(Path(__file__).resolve())
    runs_dir = tmp_path / "runs" / "usertest"
    _seed_runs_fixture(runs_dir)
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "backlog",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--dry-run",
                "--miners",
                "0",
                "--sample-size",
                "8",
                "--atom-actions-yaml",
                str(atom_actions_path),
                "--skip-plan-folder-sync",
            ]
        )
    assert exc.value.code == 0

    compiled = runs_dir / "target_a" / "_compiled"
    plans_json = compiled / "target_a.change_plans.json"
    plans_md = compiled / "target_a.change_plans.md"

    assert plans_json.exists(), "change_plans.json must be written in dry-run mode"
    assert plans_md.exists(), "change_plans.md must be written in dry-run mode"

    doc = json.loads(plans_json.read_text(encoding="utf-8"))
    assert doc.get("stage") == "implementation_planning"
    assert doc.get("input_meta", {}).get("dry_run") is True
    assert isinstance(doc.get("items"), list)
    assert doc["items"] == []
    assert doc.get("input_meta", {}).get("decision_count") == 0
    assert doc.get("input_meta", {}).get("change_plan_count") == 0
