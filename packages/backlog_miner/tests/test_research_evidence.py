from __future__ import annotations

import json
import os
import stat
import subprocess
from hashlib import sha256
from pathlib import Path

import backlog_core.stage_contracts as stage_contracts
import pytest
from agent_adapters.read_attestation import observed_read_attestation
from backlog_core import bind_plan_outcome_oracle
from runner_core.outcome_roles import run_outcome_evidence_role

import backlog_miner.research_evidence as mod
from backlog_miner.origin_evidence import (
    materialize_origin_attachments,
    origin_attachment_requirements,
)


def _falsification_replay(experiment: dict[str, object]) -> dict[str, object]:
    return {
        "experiment_id": experiment["experiment_id"],
        "command": experiment["command"],
        "declared_result": experiment["result"],
        "exit_code": experiment["exit_code"],
        "outcome": experiment["outcome"],
        "scenario_kind": experiment["scenario_kind"],
        "observable_assertion": experiment["observable_assertion"],
        "assertion_passed": True,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }


def test_falsification_attempt_binding_rejects_unrelated_refuting_experiment() -> None:
    baseline = {
        "experiment_id": "exp-baseline",
        "scenario_kind": "original_replay",
        "addresses_atom_ids": ["atom:one"],
        "command": "python tools/replay.py baseline",
        "result": "The failure is present",
        "outcome": "supports",
        "exit_code": 1,
        "observable_assertion": {
            "source": "stderr",
            "operator": "contains",
            "expected": "failure",
        },
        "artifact_refs": ["artifact:source"],
    }
    challenge = {
        **baseline,
        "experiment_id": "exp-challenge",
        "command": "python tools/replay.py alternative-removed",
        "result": "The failure remains after removing the alternative",
    }
    unrelated = {
        **baseline,
        "experiment_id": "exp-unrelated",
        "command": "python tools/unrelated.py",
        "result": "An unrelated check is green",
        "outcome": "refutes",
        "exit_code": 0,
        "observable_assertion": {
            "source": "exit_code",
            "operator": "equals",
            "expected": 0,
        },
    }
    claim = "The selected mechanism causes the failure."
    attempt = {
        "attempt_id": "attempt:selected-cause",
        "hypothesis_id": "h1",
        "claim": claim,
        "baseline_experiment_id": "exp-baseline",
        "challenge_experiment_id": "exp-challenge",
        "disproof_condition": {
            "source": "stderr",
            "operator": "not_contains",
            "expected": "failure",
        },
        "outcome": "survived",
    }
    dossier = {
        "research_status": "evidence_sufficient",
        "experiments": [baseline, challenge, unrelated],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": claim,
                "mechanism_symbols": ["core.run"],
                "supporting_evidence": ["exp-baseline", "exp-challenge"],
                "counterevidence": ["exp-unrelated"],
                "falsification_attempts": [attempt],
            }
        ],
    }
    clean_replays = {
        experiment["experiment_id"]: _falsification_replay(experiment)
        for experiment in (baseline, challenge, unrelated)
    }
    mechanism_evidence = [
        {
            "mechanism_evidence_id": "mechanism_evidence:baseline",
            "hypothesis_id": "h1",
            "experiment_ids": ["exp-baseline", "exp-challenge"],
        }
    ]
    errors: list[str] = []
    intervention = {
        "hypothesis_id": "h1",
        "attempt_id": "attempt:selected-cause",
        "intervention_receipt_id": "falsification_intervention:verified-delta",
    }

    receipts = mod._falsification_attempt_receipts(
        dossier,
        clean_replays=clean_replays,
        mechanism_evidence=mechanism_evidence,
        falsification_interventions=[intervention],
        deterministic_closures=[],
        errors=errors,
    )

    assert errors == []
    assert receipts["h1"][0]["outcome"] == "survived"
    attempt["challenge_experiment_id"] = "exp-unrelated"
    errors = []
    receipts = mod._falsification_attempt_receipts(
        dossier,
        clean_replays=clean_replays,
        mechanism_evidence=mechanism_evidence,
        falsification_interventions=[intervention],
        deterministic_closures=[],
        errors=errors,
    )
    assert receipts["h1"] == []
    assert any(
        error.startswith("falsification_attempt_unbound:h1:attempt:selected-cause")
        for error in errors
    )


def _git(command: list[str], *, cwd: Path) -> str:
    return subprocess.run(
        ["git", *command],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _baseline_repo(path: Path) -> str:
    (path / "src").mkdir(parents=True)
    (path / "src" / "core.py").write_text("def run():\n    return True\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "test_core.py").write_text(
        "def test_guarded_control():\n    assert True\n",
        encoding="utf-8",
    )
    _git(["init"], cwd=path)
    _git(["config", "user.email", "tests@example.invalid"], cwd=path)
    _git(["config", "user.name", "Tests"], cwd=path)
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", "baseline"], cwd=path)
    return _git(["rev-parse", "HEAD"], cwd=path)


def _role_contract(payload: dict[str, object]) -> dict[str, object]:
    return {**payload, "role_contract_sha256": mod._canonical_json_sha256(payload)}


def test_semantic_basis_requires_matching_relevant_falsification_attempt() -> None:
    quote = "The materialized verification path is not readable by the implementing agent."
    atom = {
        "atom_id": "atom:path",
        "text": quote,
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    experiment = {
        "experiment_id": "exp-path",
        "addresses_atom_ids": ["atom:path"],
        "positive_outcome_contract": {
            "contract_kind": "retained_harness_semantic_assertion",
            "expected_value": True,
            "semantic_relation": "required_operational_property",
            "semantic_rationale": (
                "The assertion requires the same materialized path to be readable after the fix."
            ),
            "semantic_basis": {
                "kind": "source_atom_quote",
                "atom_id": "atom:path",
                "field_path": "$.text",
                "exact_quote": quote,
            },
        },
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:path",
                "atom_sha256": mod._canonical_json_sha256(atom),
                "atom_snapshot": atom,
            }
        ]
    }
    intervention = {
        "hypothesis_id": "h1",
        "attempt_id": "attempt:path-alternative",
        "baseline_experiment_id": "exp-path",
        "intervention_receipt_id": "falsification_intervention:path",
    }
    kwargs = {
        "expected_value": True,
        "evidence_assignment": assignment,
        "planning_workspace": None,
        "inspected_file_receipts": [],
        "inspected_symbol_receipts": [],
        "falsification_interventions": [intervention],
        "hypothesis_ids": {"h1"},
        "mechanism_symbols": {"paths.materialize"},
    }

    assert mod._semantic_basis_receipt(experiment=experiment, **kwargs) is None
    experiment["positive_outcome_contract"]["adversarial_review_reference"] = (
        "attempt:path-alternative"
    )
    receipt = mod._semantic_basis_receipt(experiment=experiment, **kwargs)
    assert receipt is not None
    assert receipt["adversarial_basis"] == {
        "attempt_id": "attempt:path-alternative",
        "intervention_receipt_id": "falsification_intervention:path",
    }


def test_post_merge_replays_hash_attested_research_harness_in_clean_commit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "src").mkdir()
    (source / "src" / "core.py").write_text(
        "def run():\n    return 'bad'\n",
        encoding="utf-8",
    )
    _git(["init"], cwd=source)
    _git(["config", "user.email", "tests@example.invalid"], cwd=source)
    _git(["config", "user.name", "Tests"], cwd=source)
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "bug"], cwd=source)
    researched = _git(["rev-parse", "HEAD"], cwd=source)
    planning = tmp_path / "planning"
    research = tmp_path / "research"
    _git(["clone", str(source), str(planning)], cwd=tmp_path)
    _git(["clone", str(source), str(research)], cwd=tmp_path)
    harness = research / ".usertest_research" / "repro.py"
    harness.parent.mkdir()
    harness.write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path.cwd()))\n"
        "from src.core import run\nassert run() == 'fixed'\n",
        encoding="utf-8",
    )
    overlay = {
        key: value
        for key, value in mod._workspace_manifest(research).items()
        if key.startswith(".usertest_research/")
    }
    experiment = {
        "experiment_id": "exp-original",
        "scenario_kind": "original_replay",
        "addresses_atom_ids": ["atom:original"],
        "command": "python .usertest_research/repro.py",
        "outcome": "supports",
        "exit_code": 1,
        "observable_assertion": {
            "source": "exit_code",
            "operator": "equals",
            "expected": 1,
        },
        "positive_outcome_contract": {
            "contract_kind": "retained_harness_semantic_assertion",
            "expected_value": "fixed",
            "semantic_relation": "logical_correction_of_source_failure",
            "semantic_rationale": (
                "The source evidence names the wrong return value and the required "
                "replacement for the same default call."
            ),
            "semantic_basis": {
                "kind": "source_atom_quote",
                "atom_id": "atom:original",
                "field_path": "$.text",
                "exact_quote": "core.run returns bad; the required result is fixed",
            },
        },
    }
    stderr_path = tmp_path / "baseline-stderr.txt"
    stderr_path.write_text(
        f'Traceback (most recent call last):\n  File "{harness}", line 5\nAssertionError\n',
        encoding="utf-8",
    )
    replay = {
        "experiment_id": "exp-original",
        "executed_argv": ["python", ".usertest_research/repro.py"],
        "command_authorization": {
            "authorization_kind": "standard_test_or_research_harness",
            "executed_argv_sha256": mod._canonical_json_sha256(
                ["python", ".usertest_research/repro.py"]
            ),
            "shell": False,
            "workspace_confined": True,
        },
        "exit_code": 1,
        "workspace_dir": str(research),
        "stderr_path": str(stderr_path),
        "stdout_sha256": "a" * 64,
        "stderr_sha256": sha256(stderr_path.read_bytes()).hexdigest(),
        "assertion_passed": True,
    }
    mechanism = {
        "mechanism_evidence_id": "mechanism_evidence:harness",
        "evidence_type": "temporary_harness",
        "experiment_ids": ["exp-original"],
        "origin_atom_ids": ["atom:original"],
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
        "harness_path": ".usertest_research/repro.py",
        "adversarial_effect": "supports_selection",
    }
    errors: list[str] = []
    atom = {
        "atom_id": "atom:original",
        "text": "core.run returns bad; the required result is fixed",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:original",
                "atom_sha256": mod._canonical_json_sha256(atom),
                "atom_snapshot": atom,
            }
        ]
    }
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "usertest" / "research-run"
    run_dir.mkdir(parents=True)
    oracles = mod._outcome_oracle_receipts(
        {
            "case_id": "case:harness",
            "repo_revision": researched,
            "experiments": [experiment],
        },
        clean_replays={"exp-original": replay},
        mechanism_evidence=[mechanism],
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment=assignment,
        atom_bindings=[],
        planning_workspace=planning,
        research_workspace=research,
        overlay_manifest=overlay,
        run_dir=run_dir,
        repo_revision=researched,
        errors=errors,
    )
    assert errors == []
    assert len(oracles) == 1
    oracle = oracles[0]
    assert oracle["kind"] == "staged_replay"
    assert oracle["proof_scope"] == "behavioral"
    assert (
        stage_contracts._validate_outcome_oracles(
            {
                "case_id": "case:harness",
                "repo_revision": researched,
                "evidence_assignment": assignment,
            },
            {
                "outcome_oracles": [oracle],
                "experiments": [experiment],
                "mechanism_evidence": [mechanism],
                "control_verifications": [],
                "atom_bindings": [],
            },
            pid="problem:harness",
        )
        == []
    )

    (source / "src" / "core.py").write_text(
        "def run():\n    return 'fixed'\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "fix"], cwd=source)
    merged = _git(["rev-parse", "HEAD"], cwd=source)
    role = _role_contract(
        {
            "description": "Replay the retained original scenario.",
            "research_experiment_id": "exp-original",
            "commands": [],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0},
            ],
            "oracle": oracle,
            "required_proof_scope": "behavioral",
        }
    )
    output = runs_root / "usertest_implement" / "outcome-role.json"
    artifact = run_outcome_evidence_role(
        workspace=source,
        output_path=output,
        role="original_scenario",
        role_contract=role,
        case_id="case:harness",
        plan_revision_id="planrev:harness",
        merged_commit=merged,
        verification_contract_sha256="c" * 64,
        target_contract_sha256="d" * 64,
        verified_implementation_head=merged,
        timeout_seconds=None,
        trusted_oracle_assets_root=runs_root,
    )
    assert artifact["passed"] is True
    assert artifact["timeout_seconds"] is None
    assert artifact["commands"][0]["shell"] is False
    assert artifact["commands"][0]["argv"] == [
        "python",
        ".usertest_research/repro.py",
    ]
    assert artifact["oracle_materialization"]["cleanup_confirmed"] is True
    assert artifact["oracle_materialization"]["final_status_clean"] is True
    assert artifact["oracle_materialization"]["final_head"] == merged
    assert not (source / ".usertest_research").exists()
    assert _git(["status", "--porcelain"], cwd=source) == ""

    asset = oracle["asset"]
    assert isinstance(asset, dict)
    asset_file = runs_root / str(asset["runs_relative_path"]) / ".usertest_research" / "repro.py"
    asset_file.write_text(asset_file.read_text(encoding="utf-8") + "# tamper\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outcome_oracle_asset_hash_mismatch"):
        run_outcome_evidence_role(
            workspace=source,
            output_path=output.with_name("tampered.json"),
            role="original_scenario",
            role_contract=role,
            case_id="case:harness",
            plan_revision_id="planrev:harness",
            merged_commit=merged,
            verification_contract_sha256="c" * 64,
            target_contract_sha256="d" * 64,
            verified_implementation_head=merged,
            timeout_seconds=None,
            trusted_oracle_assets_root=runs_root,
        )
    assert not (source / ".usertest_research").exists()
    assert _git(["status", "--porcelain"], cwd=source) == ""


def test_config_oracle_closes_config_state_without_claiming_behavior(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "configs").mkdir()
    config = source / "configs" / "app.yaml"
    config.write_text("tool:\n  mode: legacy\n", encoding="utf-8")
    _git(["init"], cwd=source)
    _git(["config", "user.email", "tests@example.invalid"], cwd=source)
    _git(["config", "user.name", "Tests"], cwd=source)
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "legacy config"], cwd=source)
    researched = _git(["rev-parse", "HEAD"], cwd=source)
    planning = tmp_path / "planning"
    _git(["clone", str(source), str(planning)], cwd=tmp_path)
    experiment = {
        "experiment_id": "exp-config",
        "scenario_kind": "static_trace",
        "addresses_atom_ids": ["atom:config"],
        "command": "python tools/read_config.py",
        "outcome": "supports",
        "exit_code": 0,
        "observable_assertion": {
            "source": "stdout",
            "operator": "contains",
            "expected": "legacy",
        },
        "static_trace": {
            "deterministic": True,
            "environment_dependencies": [],
            "code_path": [
                {
                    "symbol": "config:/tool/mode",
                    "path": "configs/app.yaml",
                }
            ],
        },
        "origin_evidence_bindings": [
            {
                "role": "expected_behavior",
                "atom_id": "atom:config",
                "field_path": "$.expected_mode",
                "value": "safe",
                "value_sha256": mod._canonical_json_sha256("safe"),
            }
        ],
        "positive_outcome_contract": {
            "contract_kind": "origin_atom_exact_value",
            "atom_id": "atom:config",
            "field_path": "$.expected_mode",
            "postcondition": {
                "type": "config_state_equals",
                "mechanism_symbol": "config:/tool/mode",
                "exists": True,
                "equals": "safe",
            },
        },
    }
    replay = {
        "experiment_id": "exp-config",
        "executed_argv": ["python", "tools/read_config.py"],
        "command_authorization": {"shell": False},
        "exit_code": 0,
        "stdout_sha256": "e" * 64,
        "stderr_sha256": "f" * 64,
        "assertion_passed": True,
    }
    mechanism = {
        "mechanism_evidence_id": "mechanism_evidence:config",
        "evidence_type": "static_trace",
        "experiment_ids": ["exp-config"],
        "origin_atom_ids": ["atom:config"],
        "mechanism_symbols": ["config:/tool/mode"],
        "code_paths": [{"symbol": "config:/tool/mode", "path": "configs/app.yaml"}],
        "adversarial_effect": "supports_selection",
    }
    dossier = {
        "case_id": "case:config",
        "repo_revision": researched,
        "writes_used": False,
        "writes_purpose": ["none"],
        "experiments": [experiment],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h-config",
                "statement": "The exact config pointer retains legacy mode.",
                "mechanism_symbols": ["config:/tool/mode"],
                "supporting_evidence": ["exp-config"],
                "counterevidence": [],
                "falsification_attempts": [],
                "disposition": "primary",
            }
        ],
        "material_unknowns": [],
    }
    closure = mod._deterministic_mechanism_closure_receipts(
        dossier,
        clean_replays={"exp-config": replay},
        symbol_receipts=[{"symbol": "config:/tool/mode", "path": "configs/app.yaml"}],
        causal_links=[],
        planning_workspace=planning,
    )
    assert len(closure) == 1
    assert closure[0]["closure_basis"] == "deterministic_static_trace"
    assert closure[0]["verification_method"] == ("runner_deterministic_mechanism_closure_v1")
    assert dossier["writes_used"] is False
    assert dossier["writes_purpose"] == ["none"]
    assert _git(["status", "--porcelain"], cwd=source) == ""
    assert _git(["status", "--porcelain"], cwd=planning) == ""
    errors: list[str] = []
    atom_snapshot = {
        "expected_mode": "safe",
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    atom_receipt = {
        "atom_id": "atom:config",
        "atom_snapshot": atom_snapshot,
        "atom_sha256": mod._canonical_json_sha256(atom_snapshot),
    }
    assignment = {"atom_receipts": [atom_receipt]}
    atom_binding = {
        "experiment_id": "exp-config",
        "atom_id": "atom:config",
        "binding_role": "expected_behavior",
        "origin_atom_field_path": "$.expected_mode",
    }
    oracle = mod._outcome_oracle_receipts(
        dossier,
        clean_replays={"exp-config": replay},
        mechanism_evidence=[mechanism],
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment=assignment,
        atom_bindings=[atom_binding],
        planning_workspace=planning,
        research_workspace=None,
        overlay_manifest={},
        run_dir=tmp_path / "runs" / "usertest" / "config-run",
        repo_revision=researched,
        errors=errors,
    )[0]
    assert errors == []
    target = oracle["state_targets"][0]
    assert target["baseline_value"] == "legacy"
    assert oracle["proof_scope"] == "configuration_state"
    assert (
        stage_contracts._validate_outcome_oracles(
            {
                "case_id": "case:config",
                "repo_revision": researched,
                "evidence_assignment": assignment,
            },
            {
                "outcome_oracles": [oracle],
                "experiments": [experiment],
                "mechanism_evidence": [mechanism],
                "control_verifications": [],
                "atom_bindings": [atom_binding],
            },
            pid="problem:config",
        )
        == []
    )
    research = {
        "evidence_verification": {
            "status": "verified",
            "outcome_oracles": [oracle],
        }
    }
    plan = bind_plan_outcome_oracle(
        {
            "before_after_reproduction": {
                "research_experiment_id": "exp-config",
                "after_change": {
                    "expected_exit_code": 0,
                    "state_expectations": [
                        {
                            "target_id": target["target_id"],
                            "exists": True,
                            "equals": "safe",
                        }
                    ],
                },
            },
            "outcome_verification_roles": {
                "original_scenario": {"description": "Verify config state."},
                "live": None,
                "mitigation_effect": None,
                "recurrence": None,
            },
        },
        research=research,
    )
    role = _role_contract(dict(plan["outcome_verification_roles"]["original_scenario"]))
    config.write_text("tool:\n  mode: safe\n", encoding="utf-8")
    _git(["add", "-A"], cwd=source)
    _git(["commit", "-m", "safe config"], cwd=source)
    merged = _git(["rev-parse", "HEAD"], cwd=source)
    artifact = run_outcome_evidence_role(
        workspace=source,
        output_path=tmp_path / "runs" / "usertest_implement" / "config-role.json",
        role="original_scenario",
        role_contract=role,
        case_id="case:config",
        plan_revision_id="planrev:config",
        merged_commit=merged,
        verification_contract_sha256="1" * 64,
        target_contract_sha256="2" * 64,
        verified_implementation_head=merged,
        timeout_seconds=None,
    )
    assert artifact["passed"] is True
    assert artifact["commands"] == []
    assert artifact["proof_scope"] == "configuration_state"
    assert artifact["oracle_states"][0]["value"] == "safe"
    assert _git(["status", "--porcelain"], cwd=source) == ""

    forged_payload = {key: value for key, value in role.items() if key != "role_contract_sha256"}
    forged_payload["required_proof_scope"] = "behavioral"
    forged = _role_contract(forged_payload)
    with pytest.raises(ValueError, match="outcome_role_oracle_scope_mismatch"):
        run_outcome_evidence_role(
            workspace=source,
            output_path=tmp_path / "runs" / "usertest_implement" / "forged.json",
            role="original_scenario",
            role_contract=forged,
            case_id="case:config",
            plan_revision_id="planrev:config",
            merged_commit=merged,
            verification_contract_sha256="1" * 64,
            target_contract_sha256="2" * 64,
            verified_implementation_head=merged,
            timeout_seconds=None,
        )


def test_verified_mechanism_identity_is_stable_across_case_provenance() -> None:
    def projection(
        hypothesis_id: str,
        *,
        evidence_id: str,
        control_id: str,
        probe_slot: str,
    ) -> tuple[dict[str, object] | None, str | None, dict[str, object] | None, str | None]:
        return mod._verified_mechanism_projection(
            {
                "root_cause_hypotheses": [
                    {
                        "hypothesis_id": hypothesis_id,
                        "statement": f"Case-specific prose for {hypothesis_id}",
                        "mechanism_symbols": ["router.route"],
                    }
                ]
            },
            mechanism_evidence=[
                {
                    "hypothesis_id": hypothesis_id,
                    "mechanism_symbols": ["router.route"],
                    "mechanism_evidence_id": evidence_id,
                    "code_paths": [{"symbol": "router.route", "path": "src/router.py"}],
                }
            ],
            control_verifications=[
                {
                    "hypothesis_id": hypothesis_id,
                    "mechanism_symbols": ["router.route"],
                    "control_verification_id": control_id,
                    "controlled_input_difference": {
                        "verification_method": ("python_ast_explicit_argument_delta_v1"),
                        "difference": {
                            "mechanism_symbol": "router.route",
                            "slot": probe_slot,
                        },
                    },
                }
            ],
            falsification_interventions=[],
            deterministic_closures=[],
        )

    first = projection(
        "hypothesis:case-a",
        evidence_id="mechanism_evidence:case-a",
        control_id="control_verification:case-a",
        probe_slot="keyword:policy",
    )
    second = projection(
        "hypothesis:case-b",
        evidence_id="mechanism_evidence:case-b",
        control_id="control_verification:case-b",
        probe_slot="keyword:fixture",
    )

    assert (
        first[0]
        == second[0]
        == {
            "schema_version": 2,
            "mechanism_symbols": ["router.route"],
            "code_paths": [{"symbol": "router.route", "path": "src/router.py"}],
        }
    )
    assert first[1] == second[1]
    assert first[2] != second[2]
    assert first[3] != second[3]


def test_persisted_origin_attachment_receipt_revalidates_chunks_and_reads(
    tmp_path: Path,
) -> None:
    origin_run = tmp_path / "runs" / "origin"
    origin_run.mkdir(parents=True)
    signature = "ONLY_MIDDLE_RESEARCH_SIGNATURE"
    artifact = origin_run / "agent_stderr.txt"
    artifact.write_text(("prefix\n" * 2_000) + signature + ("\nsuffix" * 2_000))
    workspace = tmp_path / "research-workspace"
    manifest = materialize_origin_attachments(
        atoms=[
            {
                "atom_id": "atom:origin",
                "run_dir": str(origin_run),
                "attachments": [
                    {
                        "artifact_ref": {
                            "path": artifact.name,
                            "sha256": sha256(artifact.read_bytes()).hexdigest(),
                            "size_bytes": artifact.stat().st_size,
                        }
                    }
                ],
            }
        ],
        workspace_dir=workspace,
        source_root=tmp_path,
        relative_root=Path(".usertest_research") / "origin_evidence",
    )
    events: list[dict[str, object]] = []
    attestations: list[dict[str, object]] = []
    requirements = origin_attachment_requirements(manifest)
    for requirement in requirements:
        chunk = workspace / str(requirement["file"])
        event: dict[str, object] = {
            "ts": "2026-07-10T00:00:00Z",
            "type": "read_file",
            "data": {
                "path": str(requirement["file"]),
                "read_source": "tool",
                "source_exit_code": 0,
                **observed_read_attestation(
                    path=chunk,
                    observed_text=chunk.read_text(encoding="utf-8"),
                    source_exit_code=0,
                    allow_partial=False,
                ),
            },
        }
        events.append(event)
        attestations.append(
            {
                "artifact_sha256": requirement["artifact_sha256"],
                "file": requirement["file"],
                "file_sha256": requirement["sha256"],
                "file_size_bytes": requirement["size_bytes"],
                "read_event_index": len(events) - 1,
                "read_event_sha256": mod._canonical_json_sha256(event),
            }
        )
    assignment = {"origin_attachment_evidence": manifest}
    receipt = {
        "origin_attachment_evidence": manifest,
        "origin_attachment_read_attestations": attestations,
    }

    assert (
        mod._persisted_origin_attachment_errors(
            assignment=assignment,
            receipt=receipt,
            research_workspace=workspace,
            persisted_events=events,
        )
        == []
    )

    middle_requirement = next(
        requirement
        for requirement in requirements
        if signature in (workspace / str(requirement["file"])).read_text(encoding="utf-8")
    )
    (workspace / str(middle_requirement["file"])).write_text("tampered\n")
    errors = mod._persisted_origin_attachment_errors(
        assignment=assignment,
        receipt=receipt,
        research_workspace=workspace,
        persisted_events=events,
    )
    assert any("origin_attachment_chunk_changed" in error for error in errors)


def test_runner_materialized_origin_evidence_is_not_misclassified_as_agent_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _baseline_repo(source)
    research = tmp_path / "research"
    baseline = tmp_path / "baseline"
    for destination in (research, baseline):
        subprocess.run(
            ["git", "clone", str(source), str(destination)],
            check=True,
            capture_output=True,
        )
    origin_run = tmp_path / "runs" / "origin"
    origin_run.mkdir(parents=True)
    artifact = origin_run / "agent_stderr.txt"
    artifact.write_text("retained failure\n")
    manifest = materialize_origin_attachments(
        atoms=[
            {
                "atom_id": "atom:origin",
                "run_dir": str(origin_run),
                "attachments": [
                    {
                        "artifact_ref": {
                            "path": artifact.name,
                            "sha256": sha256(artifact.read_bytes()).hexdigest(),
                        }
                    }
                ],
            }
        ],
        workspace_dir=research,
        source_root=tmp_path,
        relative_root=Path(".usertest_research") / "origin_evidence",
    )
    assert manifest["errors"] == []

    errors, overlay = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert errors == []
    assert overlay["research_overlay_paths"] == []
    assert overlay["runner_materialized_evidence_paths"]
    assert mod._verified_diff_classification("no_changes", overlay) == "no_changes"


def _baseline_repo_commit_existing(path: Path, message: str) -> str:
    _git(["init"], cwd=path)
    _git(["config", "user.email", "tests@example.invalid"], cwd=path)
    _git(["config", "user.name", "Tests"], cwd=path)
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", message], cwd=path)
    return _git(["rev-parse", "HEAD"], cwd=path)


def _causal_control_repo(path: Path) -> None:
    (path / "src").mkdir(parents=True)
    (path / "src" / "core.py").write_text(
        "def run(*, guarded=False, extra=False):\n"
        "    if not guarded:\n"
        "        raise RuntimeError('reported failure')\n"
        "    return True\n",
        encoding="utf-8",
    )
    (path / "tests").mkdir()
    (path / "tests" / "test_core.py").write_text(
        "from src.core import run\n\n"
        "def test_reported_failure():\n"
        "    run()\n\n"
        "def test_unrelated_same_file():\n"
        "    assert 2 + 2 == 4\n\n"
        "def test_shadowed_mechanism_name():\n"
        "    run = lambda: True\n"
        "    assert run() is True\n\n"
        "def test_guarded_control():\n"
        "    assert run(guarded=True) is True\n\n"
        "def test_same_input_control():\n"
        "    run()\n\n"
        "def test_two_input_control():\n"
        "    assert run(guarded=True, extra=True) is True\n",
        encoding="utf-8",
    )
    (path / "tests" / "test_other.py").write_text(
        "def test_unrelated_other_file():\n    assert 'ready'.upper() == 'READY'\n",
        encoding="utf-8",
    )
    _git(["init"], cwd=path)
    _git(["config", "user.email", "tests@example.invalid"], cwd=path)
    _git(["config", "user.name", "Tests"], cwd=path)
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", "causal control baseline"], cwd=path)


def _control_dossier(control_target: str) -> tuple[dict[str, object], dict[str, dict]]:
    support_command = "pytest -q tests/test_core.py::test_reported_failure"
    control_command = f"pytest -q {control_target}"
    dossier: dict[str, object] = {
        "experiments": [
            {
                "experiment_id": "support",
                "scenario_kind": "original_replay",
                "command": support_command,
                "outcome": "supports",
                "exit_code": 1,
                "addresses_atom_ids": ["atom:support"],
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
            },
            {
                "experiment_id": "control",
                "scenario_kind": "control",
                "command": control_command,
                "outcome": "refutes",
                "exit_code": 0,
                "addresses_atom_ids": ["atom:support"],
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "control_relationship": {
                    "supports_experiment_id": "support",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "guarded input",
                    "expected_difference": "guarded call succeeds",
                },
            },
        ],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "supporting_evidence": ["support"],
                "counterevidence": ["control"],
                "mechanism_symbols": ["core.run"],
            }
        ],
    }
    replays = {
        "support": {
            "executed_argv": mod._parse_replay_argv(support_command),
            "exit_code": 1,
            "assertion_passed": True,
            "stdout_sha256": "1" * 64,
            "stderr_sha256": "2" * 64,
        },
        "control": {
            "executed_argv": mod._parse_replay_argv(control_command),
            "exit_code": 0,
            "assertion_passed": True,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
        },
    }
    return dossier, replays


def test_complete_manifest_detects_staged_hidden_and_untracked_production_edits(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    (research / "src" / "core.py").write_text(
        "def run():\n    return False\n",
        encoding="utf-8",
    )
    _git(["add", "src/core.py"], cwd=research)
    _git(["update-index", "--assume-unchanged", "src/core.py"], cwd=research)
    (research / "tests" / "fake_test.py").write_text(
        "def test_fake():\n    assert True\n",
        encoding="utf-8",
    )
    (research / ".usertest_research").mkdir()
    (research / ".usertest_research" / "notes.txt").write_text(
        "allowed research overlay\n",
        encoding="utf-8",
    )

    errors, receipt = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "baseline_file_changed:src/core.py" in errors
    assert "untracked_workspace_file:tests/fake_test.py" in errors
    assert "git_index_changed" in errors
    assert receipt["git_index_changed"] is True
    assert receipt["research_overlay_paths"] == [".usertest_research/notes.txt"]


def test_suspicious_diff_classification_is_monotonic() -> None:
    clean_overlay = {
        "changed_baseline_paths": [],
        "research_overlay_paths": [],
        "suspicious_extra_paths": [],
        "git_index_changed": False,
    }

    assert (
        mod._verified_diff_classification("suspicious_implementation", clean_overlay)
        == "suspicious_implementation"
    )


def test_canonical_manifest_detects_mode_symlink_and_index_state(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)

    _git(["update-index", "--chmod=+x", "src/core.py"], cwd=research)
    symlink_path = research / ".usertest_research" / "source-link.py"
    symlink_path.parent.mkdir()
    try:
        os.symlink(research / "src" / "core.py", symlink_path)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    errors, receipt = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "git_index_changed" in errors
    assert "untracked_workspace_file:.usertest_research/source-link.py" in errors
    assert receipt["git_index_changed"] is True
    assert receipt["baseline_git_index_sha256"] != receipt["research_git_index_sha256"]


def test_canonical_manifest_detects_filesystem_mode_change(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    source = research / "src" / "core.py"
    original_mode = stat.S_IMODE(source.stat().st_mode)
    os.chmod(source, original_mode & ~stat.S_IWUSR)
    changed_mode = stat.S_IMODE(source.stat().st_mode)
    if changed_mode == original_mode:
        pytest.skip("filesystem does not expose chmod changes")

    errors, _ = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )

    assert "baseline_file_changed:src/core.py" in errors


def test_one_agent_event_cannot_attest_two_experiments() -> None:
    dossier = {
        "experiments": [
            {
                "experiment_id": "support",
                "command": "pytest -q",
                "exit_code": 1,
                "outcome": "supports",
                "artifact_refs": ["artifact:one"],
            },
            {
                "experiment_id": "control",
                "command": "pytest -q",
                "exit_code": 1,
                "outcome": "refutes",
                "artifact_refs": ["artifact:one"],
            },
        ]
    }
    event = {"type": "run_command", "data": {"command": "pytest -q", "exit_code": 1}}
    clean_replays = {
        experiment_id: {
            "experiment_id": experiment_id,
            "command": "pytest -q",
            "exit_code": 1,
            "artifact_refs": ["artifact:one"],
        }
        for experiment_id in ("support", "control")
    }
    errors: list[str] = []

    receipts, _ = mod._experiment_receipts(
        dossier,
        events=[event],
        artifact_keys={"artifact:one"},
        clean_replays=clean_replays,
        errors=errors,
    )

    assert [receipt["experiment_id"] for receipt in receipts] == ["support"]
    assert "experiment_command_not_observed:control" in errors


def test_clean_replay_rejects_agent_claim_that_baseline_does_not_reproduce(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    revision = _baseline_repo(baseline)
    dossier = {
        "artifact_refs": [],
        "experiments": [
            {
                "experiment_id": "claimed-failure",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "pytest -q -k guarded_control",
                "result": "The control allegedly fails",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": [],
            }
        ],
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[baseline],
            source_identity=baseline,
        ),
    )

    assert receipts["claimed-failure"]["exit_code"] == 0
    assert any(error.startswith("experiment_replay_exit_mismatch") for error in errors)
    assert "experiment_observable_assertion_failed:claimed-failure" in errors


def test_clean_replay_copies_hash_attested_overlay_harness(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    revision = _baseline_repo(baseline)
    research = tmp_path / "research"
    _git(["clone", str(baseline), str(research)], cwd=tmp_path)
    harness = research / ".usertest_research" / "test_repro.py"
    harness.parent.mkdir()
    harness.write_text(
        "from pathlib import Path\n\n"
        "def test_overlay_repro():\n"
        "    assert 'def run' in Path('src/core.py').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    overlay_errors, overlay = mod._workspace_overlay_errors(
        research_workspace=research,
        baseline_workspace=baseline,
    )
    assert overlay_errors == []
    dossier = {
        "artifact_refs": [],
        "experiments": [
            {
                "experiment_id": "overlay-repro",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "python -m pytest -q .usertest_research/test_repro.py",
                "result": "The isolated overlay harness passes",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": [],
            }
        ],
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=research,
        overlay_manifest=overlay["research_overlay_manifest"],
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[research],
            source_identity=research,
        ),
    )

    receipt = receipts["overlay-repro"]
    replay_harness = Path(receipt["workspace_dir"]) / ".usertest_research/test_repro.py"
    assert replay_harness.is_file()
    assert receipt["overlay_manifest_sha256"] == overlay["research_overlay_manifest_sha256"]
    assert receipt["post_replay_mutations"] is False
    assert errors == []


def test_clean_replay_detects_persisted_tracked_file_mutation(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    revision = _baseline_repo(baseline)
    mutation_test = baseline / "tests" / "test_mutation.py"
    mutation_test.write_text(
        "from pathlib import Path\n\n"
        "def test_mutates_checkout():\n"
        "    Path('src/core.py').write_text('mutated\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(["add", "tests/test_mutation.py"], cwd=baseline)
    _git(["commit", "-m", "mutation fixture"], cwd=baseline)
    revision = _git(["rev-parse", "HEAD"], cwd=baseline)
    dossier = {
        "artifact_refs": [],
        "experiments": [
            {
                "experiment_id": "mutating-replay",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "pytest -q tests/test_mutation.py",
                "result": "The test passes after mutating the checkout",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": [],
            }
        ],
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=30,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[baseline],
            source_identity=baseline,
        ),
    )

    assert receipts["mutating-replay"]["post_replay_mutations"] is True
    assert "experiment_replay_workspace_mutated:mutating-replay" in errors


def test_partial_read_cannot_attest_unobserved_symbol(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    revision = _baseline_repo(workspace)
    del revision
    source = workspace / "src" / "core.py"
    source.write_text(
        "def observed():\n    return True\n\ndef unseen():\n    return False\n",
        encoding="utf-8",
    )
    _git(["add", "src/core.py"], cwd=workspace)
    _git(["commit", "-m", "two symbols"], cwd=workspace)
    observed = "def observed():\n    return True\n"
    attestation = observed_read_attestation(
        path=source,
        observed_text=observed,
        source_exit_code=0,
        allow_partial=True,
    )
    event = {
        "type": "read_file",
        "data": {
            "path": "src/core.py",
            "bytes": source.stat().st_size,
            "read_source": "tool",
            "source_exit_code": 0,
            **attestation,
        },
    }
    dossier = {
        "inspected_files": ["src/core.py"],
        "inspected_symbols": ["core.unseen"],
    }
    errors: list[str] = []

    files, symbols = mod._inspection_receipts(
        dossier,
        workspace=workspace,
        events=[event],
        errors=errors,
    )

    assert len(files) == 1
    assert symbols == []
    assert "inspected_symbol_unresolved:core.unseen" in errors


def test_unrelated_assertion_failure_has_no_mechanism_causal_link(tmp_path: Path) -> None:
    output = "tests/test_repro.py:4: in test_repro\n    assert False\nE   assert False\n"

    assert (
        mod._causal_trace_match(
            output=output,
            relative_path="src/core.py",
            symbol="core.run",
        )
        is None
    )
    linked = mod._causal_trace_match(
        output='  File "/workspace/src/core.py", line 2, in run\n',
        relative_path="src/core.py",
        symbol="core.run",
    )
    assert linked is not None
    assert linked[0] == "python_traceback"


def test_model_overlay_cannot_print_its_own_mechanism_causal_link(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "stdout.txt"
    output_path.write_text(
        '  File "/workspace/src/core.py", line 2, in run\n',
        encoding="utf-8",
    )
    dossier = {
        "experiments": [
            {
                "experiment_id": "self-authored-replay",
                "scenario_kind": "faithful_replay",
                "command": "pytest -q .usertest_research/test_fake_trace.py",
                "outcome": "supports",
            }
        ],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "supporting_evidence": ["self-authored-replay"],
                "mechanism_symbols": ["core.run"],
            }
        ],
    }
    errors: list[str] = []

    links = mod._causal_link_receipts(
        dossier,
        clean_replays={
            "self-authored-replay": {
                "executed_argv": [
                    "pytest",
                    "-q",
                    ".usertest_research/test_fake_trace.py",
                ],
                "stdout_path": str(output_path),
                "stderr_path": str(tmp_path / "missing-stderr.txt"),
            }
        },
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert links == []
    assert any("model_overlay_untrusted" in error for error in errors)
    assert "mechanism_causal_trace_missing:h1:core.run" in errors


def test_one_replay_cannot_cover_unrelated_commandless_atoms_by_exit_code(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin.json"
    origin.write_text('{"message":"two unrelated failures"}', encoding="utf-8")
    dossier = {
        "experiments": [
            {
                "experiment_id": "generic-failure",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:one", "atom:two"],
                "command": "pytest -q tests/test_one.py",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
            }
        ]
    }
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:one", "atom:two"],
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_snapshot": {
                    "atom_id": atom_id,
                    "text": text,
                    "exit_code": 1,
                },
                "artifact_receipts": [{"path": str(origin)}],
            }
            for atom_id, text in (
                ("atom:one", "Database migration failed"),
                ("atom:two", "Browser launch failed"),
            )
        ],
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert any("experiment_not_bound_to_atom:generic-failure:atom:one" in e for e in errors)
    assert any("experiment_not_bound_to_atom:generic-failure:atom:two" in e for e in errors)
    assert "supporting_experiments_do_not_cover_origin_atoms" in errors


def test_persisted_receipt_json_projection_is_stable() -> None:
    assert mod._canonical_json_sha256({"b": 2, "a": 1}) == mod._canonical_json_sha256(
        json.loads('{"a": 1, "b": 2}')
    )


def test_inspected_symbol_supports_exact_python_import_and_constant_bindings() -> None:
    content = (
        "import os as operating_system\n"
        "from pathlib import Path as RepoPath\n"
        "DEFAULT_LIMIT = 3\n"
        "class Settings:\n"
        "    ENABLED: bool = True\n"
    )

    for symbol in (
        "module.operating_system",
        "module.RepoPath",
        "module.DEFAULT_LIMIT",
        "module.Settings.ENABLED",
    ):
        assert mod._symbol_definition_exists(
            path="src/module.py",
            content=content,
            symbol=symbol,
        )
    assert not mod._symbol_definition_exists(
        path="src/module.py",
        content=content,
        symbol="module.MISSING",
    )


@pytest.mark.parametrize(
    ("path", "content", "symbol"),
    [
        (
            "config.json",
            '{"tool":{"with/slash":{"~key":true}}}',
            "config:/tool/with~1slash/~0key",
        ),
        (
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = "-q"\n',
            "config:/tool/pytest/ini_options/addopts",
        ),
        (
            "pipeline.yaml",
            "pipelines:\n  - name: primary\n",
            "config:/pipelines/0/name",
        ),
    ],
)
def test_inspected_symbol_supports_unambiguous_rfc6901_config_keys(
    path: str,
    content: str,
    symbol: str,
) -> None:
    assert mod._symbol_definition_exists(path=path, content=content, symbol=symbol)


@pytest.mark.parametrize(
    ("path", "content", "symbol"),
    [
        ("config.json", '{"tool":1,"tool":2}', "config:/tool"),
        ("config.yaml", "tool: 1\ntool: 2\n", "config:/tool"),
        ("config.json", '{"tool":{"value":1}}', "tool.value"),
        ("config.json", '{"tool":{"value":1}}', "config:/tool/~2value"),
    ],
)
def test_config_symbol_fails_closed_on_duplicates_or_ambiguous_syntax(
    path: str,
    content: str,
    symbol: str,
) -> None:
    assert not mod._symbol_definition_exists(path=path, content=content, symbol=symbol)


def test_replay_command_parser_rejects_shell_and_control_injection() -> None:
    assert mod._parse_replay_argv("pytest -q tests/test_core.py") == [
        "pytest",
        "-q",
        "tests/test_core.py",
    ]
    assert mod._parse_replay_argv("pdm run python -m pytest -q") == [
        "pdm",
        "run",
        "python",
        "-m",
        "pytest",
        "-q",
    ]
    assert mod._parse_replay_argv(r"python .usertest_research\route_contract_probe.py") == [
        "python",
        ".usertest_research/route_contract_probe.py",
    ]
    assert mod._parse_replay_argv(
        r'pdm run python ".usertest_research\route contract probe.py"'
    ) == [
        "pdm",
        "run",
        "python",
        ".usertest_research/route contract probe.py",
    ]
    assert mod._parse_replay_argv(
        r"pytest packages\runner_core\tests\test_codex_execpolicy.py"
    ) == [
        "pytest",
        "packages/runner_core/tests/test_codex_execpolicy.py",
    ]
    assert mod._parse_replay_argv(
        r"python -m pytest packages\runner_core\tests\test_codex_execpolicy.py"
    ) == [
        "python",
        "-m",
        "pytest",
        "packages/runner_core/tests/test_codex_execpolicy.py",
    ]
    assert mod._parse_replay_argv(
        r"pytest packages\runner_core\tests\test_x.py::test_path[param\value]"
    ) == [
        "pytest",
        r"packages/runner_core/tests/test_x.py::test_path[param\value]",
    ]
    assert mod._parse_replay_argv('pytest -q -k="foo or bar"') == [
        "pytest",
        "-q",
        "-k=foo or bar",
    ]
    assert mod._parse_replay_argv('python -m pytest --override-ini="addopts=-ra -q"') == [
        "python",
        "-m",
        "pytest",
        "--override-ini=addopts=-ra -q",
    ]
    for command in (
        "pytest -q\nWrite-Output forged",
        "pytest -q\r\nwhoami",
        "pytest -q; whoami",
        "pytest -q | whoami",
        "pytest -q && whoami",
        "pytest -q > forged.txt",
        "pytest -q `whoami`",
        r"pytest tests/foo\ bar.py",
    ):
        assert mod._parse_replay_argv(command) is None
    assert mod._replay_argv_is_workspace_confined(["pytest", "-q", "tests/test_core.py"])
    assert not mod._replay_argv_is_workspace_confined(
        ["pytest", "-q", "../../outside/test_payload.py"]
    )
    assert not mod._replay_argv_is_workspace_confined(["pytest", "--rootdir=C:\\outside"])
    for command in (
        r"python .usertest_research\..\outside.py",
        r"python C:\outside\probe.py",
        r"pytest ..\outside\test_probe.py",
        r"pytest C:\outside\test_probe.py",
        r"pytest C:outside\test_probe.py",
        r"pytest --rootdir=C:outside tests\test_probe.py",
        r"pytest --basetemp=\outside tests\test_probe.py",
    ):
        argv = mod._parse_replay_argv(command)
        assert argv is None or not mod._replay_argv_is_workspace_confined(argv)


def test_practical_config_cli_replay_proves_wrong_value_to_correct_value(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    tool = baseline / "tools" / "show_mode.py"
    tool.parent.mkdir()
    tool.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "print(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['mode'])\n",
        encoding="utf-8",
    )
    (baseline / "bad.json").write_text('{"mode":"bad"}\n', encoding="utf-8")
    (baseline / "correct.json").write_text('{"mode":"correct"}\n', encoding="utf-8")
    revision = _baseline_repo_commit_existing(baseline, "practical config cli")
    dossier = {
        "inspected_files": ["tools/show_mode.py"],
        "experiments": [
            {
                "experiment_id": "support",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:config"],
                "command": "python tools/show_mode.py bad.json",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
            },
            {
                "experiment_id": "control",
                "scenario_kind": "control",
                "addresses_atom_ids": ["atom:config"],
                "command": "python tools/show_mode.py correct.json",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "correct",
                },
            },
        ],
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:config",
                "atom_sha256": "a" * 64,
                "atom_snapshot": {
                    "atom_id": "atom:config",
                    "command": "python tools/show_mode.py bad.json",
                    "output_excerpt": "bad",
                },
            }
        ]
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=300.0,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )

    assert errors == []
    assert receipts["support"]["command_authorization"]["authorization_kind"] == (
        "immutable_source_command"
    )
    assert receipts["support"]["command_authorization"]["origin_atom_field_path"] == ("$.command")
    assert receipts["control"]["command_authorization"]["authorization_kind"] == (
        "declared_inspected_repository_entrypoint"
    )
    difference_errors: list[str] = []
    difference = mod._observable_controlled_difference(
        hypothesis_id="h1",
        control_id="control",
        support=dossier["experiments"][0],
        control=dossier["experiments"][1],
        support_replay=receipts["support"],
        control_replay=receipts["control"],
        errors=difference_errors,
    )
    assert difference_errors == []
    assert difference is not None
    assert difference["difference_kind"] == "wrong_value_corrected"
    assert difference["support_expected_sha256"] == mod._canonical_json_sha256("bad")
    assert difference["control_expected_sha256"] == mod._canonical_json_sha256("correct")


def test_practical_repo_cli_is_rejected_without_source_or_inspection_binding(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    tool = baseline / "tools" / "show_mode.py"
    tool.parent.mkdir()
    tool.write_text("print('bad')\n", encoding="utf-8")
    revision = _baseline_repo_commit_existing(baseline, "unbound practical cli")
    dossier = {
        "experiments": [
            {
                "experiment_id": "unbound",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:other"],
                "command": "python tools/show_mode.py",
                "outcome": "supports",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
            }
        ]
    }
    assignment = {
        "atom_receipts": [
            {
                "atom_id": "atom:other",
                "atom_snapshot": {"command": "python -m unrelated"},
            }
        ]
    }
    errors: list[str] = []

    receipts = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=300.0,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )

    assert receipts == {}
    assert errors == ["experiment_command_not_replay_allowlisted:unbound"]


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "-q", "-kguarded", "tests/test_core.py::test_guarded_control"],
        ["pytest", "-q", "-mcontrol", "tests/test_core.py::test_guarded_control"],
        ["pytest", "-q", "tests/test_core.py::test_guarded_control[param]"],
    ],
)
def test_exact_pytest_selector_fails_closed_for_ambiguous_or_parameterized_selection(
    argv: list[str],
) -> None:
    assert mod._exact_pytest_selector(argv) is None


@pytest.mark.parametrize(
    "control_target",
    [
        "tests/test_core.py::test_unrelated_same_file",
        "tests/test_core.py::test_shadowed_mechanism_name",
        "tests/test_other.py::test_unrelated_other_file",
    ],
)
def test_unrelated_passing_test_cannot_be_causal_counterevidence(
    tmp_path: Path,
    control_target: str,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier(control_target)
    errors: list[str] = []

    selections, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert len(selections) == 2
    assert controls == []
    assert "causal_control_mechanism_not_called:h1:control:core.run" in errors
    assert "causal_control_mechanism_coverage_missing:h1:control" in errors


def test_focused_guarded_control_calling_same_mechanism_is_verified(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    errors: list[str] = []

    selections, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert errors == []
    assert {selection["experiment_id"] for selection in selections} == {
        "support",
        "control",
    }
    assert all(
        selection["mechanism_touches"][0]["symbol"] == "core.run" for selection in selections
    )
    assert len(controls) == 1
    control = controls[0]
    assert control["verification_method"] == "pytest_ast_controlled_difference_v2"
    assert control["controlled_input_difference"] == {
        "verification_method": "python_ast_explicit_argument_delta_v1",
        "difference_count": 1,
        "difference": {
            "mechanism_symbol": "core.run",
            "slot": "keyword:guarded",
            "difference_kind": "added_in_control",
            "support_argument": None,
            "control_argument": {
                "slot": "keyword:guarded",
                "expression": "True",
                "ast_sha256": mod.sha256(b"Constant(value=True)").hexdigest(),
            },
        },
    }
    assert control["observable_difference"]["difference_kind"] == "failing_exit_to_zero"
    assert control["observable_difference"]["support"]["exit_code"] == 1
    assert control["observable_difference"]["control"]["exit_code"] == 0
    assert control["adversarial_effect"] == "limits_scope"
    assert control["control_verification_id"] == mod._content_addressed_receipt_id(
        "control_verification",
        control,
        "control_verification_id",
    )

    failure_paths = mod._failure_path_receipts(
        dossier,
        test_selections=selections,
        control_verifications=controls,
        errors=errors,
    )
    assert errors == []
    assert len(failure_paths) == 1
    assert failure_paths[0]["path_name"] == ("tests/test_core.py::test_reported_failure")
    assert failure_paths[0]["origin_atom_ids"] == ["atom:support"]
    assert failure_paths[0]["failure_path_id"] == mod._content_addressed_receipt_id(
        "failure_path",
        failure_paths[0],
        "failure_path_id",
    )


@pytest.mark.parametrize(
    ("control_target", "expected_error"),
    [
        (
            "tests/test_core.py::test_same_input_control",
            "causal_control_requires_exactly_one_structural_difference:h1:control:0",
        ),
        (
            "tests/test_core.py::test_two_input_control",
            "causal_control_requires_exactly_one_structural_difference:h1:control:2",
        ),
    ],
)
def test_control_requires_exactly_one_runner_observed_input_delta(
    tmp_path: Path,
    control_target: str,
    expected_error: str,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier(control_target)
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert controls == []
    assert expected_error in errors


def test_falsification_challenge_requires_runner_observed_causal_input_delta(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_same_input_control")
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["statement"] = "The default core.run input causes the failure."
    hypothesis["supporting_evidence"] = ["support", "control"]
    hypothesis["counterevidence"] = []
    hypothesis["falsification_attempts"] = [
        {
            "attempt_id": "attempt:same-input",
            "hypothesis_id": "h1",
            "claim": hypothesis["statement"],
            "baseline_experiment_id": "support",
            "challenge_experiment_id": "control",
            "disproof_condition": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 0,
            },
            "outcome": "survived",
        }
    ]
    challenge = dossier["experiments"][1]
    assert isinstance(challenge, dict)
    challenge["outcome"] = "supports"
    challenge["exit_code"] = 1
    challenge["observable_assertion"] = {
        "source": "exit_code",
        "operator": "equals",
        "expected": 1,
    }
    replays["control"]["exit_code"] = 1
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert receipts == []
    assert any(
        error.startswith("falsification_intervention_unverified:h1:attempt:same-input")
        for error in errors
    )


def test_model_control_prose_cannot_turn_an_invalid_pair_into_causal_proof(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_same_input_control")
    control = dossier["experiments"][1]
    assert isinstance(control, dict)
    relationship = control["control_relationship"]
    assert isinstance(relationship, dict)
    relationship["controlled_variable"] = "author insists this is different"
    relationship["expected_difference"] = "author insists this succeeds"
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert controls == []
    assert "causal_control_requires_exactly_one_structural_difference:h1:control:0" in errors


def test_control_requires_complementary_runner_observation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    control = dossier["experiments"][1]
    assert isinstance(control, dict)
    control["exit_code"] = 2
    assertion = control["observable_assertion"]
    assert isinstance(assertion, dict)
    assertion["expected"] = 2
    replays["control"]["exit_code"] = 2
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert controls == []
    assert "causal_control_observable_not_complementary:h1:control" in errors


def test_supporting_experiment_must_reproduce_the_atom_symptom(tmp_path: Path) -> None:
    origin = tmp_path / "origin.json"
    origin.write_text(
        '{"error":"shell_probe_failed prevented the mission"}',
        encoding="utf-8",
    )
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:shell"],
        "atom_receipts": [
            {
                "atom_id": "atom:shell",
                "atom_snapshot": {
                    "atom_id": "atom:shell",
                    "command": "pytest -q",
                    "text": "shell_probe_failed prevented the mission",
                    "exit_code": 1,
                },
                "artifact_receipts": [{"path": str(origin)}],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "absence-is-not-support",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:shell"],
                "command": "pytest -q",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "combined",
                    "operator": "not_contains",
                    "expected": "shell_probe_failed",
                },
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert "experiment_not_bound_to_atom:absence-is-not-support:atom:shell" in errors
    assert "supporting_experiments_do_not_cover_origin_atoms" in errors

    dossier["experiments"][0]["observable_assertion"]["operator"] = "contains"
    errors = []
    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)
    assert len(bindings) == 1
    assert bindings[0]["experiment_id"] == "absence-is-not-support"
    assert bindings[0]["atom_id"] == "atom:shell"
    assert bindings[0]["match_kind"] == "command_and_atom_evidence_symptom"
    assert bindings[0]["origin_atom_field_path"] == "$.text"
    assert bindings[0]["origin_artifact_path"] == str(origin)
    assert bindings[0]["origin_artifact_sha256"] == mod._sha256_path(origin)
    assert errors == []


def test_harness_call_discard_with_hard_coded_symptom_is_not_mechanism_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "replay"
    harness = workspace / ".usertest_research" / "probe.py"
    harness.parent.mkdir(parents=True)
    harness.write_text(
        "from core import run\nrun()\nprint('shell_probe_failed')\n",
        encoding="utf-8",
    )
    replay = {
        "executed_argv": ["python", ".usertest_research/probe.py"],
        "workspace_dir": str(workspace),
    }

    path, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "shell_probe_failed",
        },
    )

    assert path == ".usertest_research/probe.py"
    assert touched == []
    assert link is None

    harness.write_text(
        "from core import run\nprint(f'{run()} :: shell_probe_failed')\n",
        encoding="utf-8",
    )
    _, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "shell_probe_failed",
        },
    )
    assert touched == []
    assert link is None

    harness.write_text(
        "from core import run\nresult = run()\nprint(result)\n",
        encoding="utf-8",
    )
    _, touched, link = mod._harness_mechanism_touches(
        replay=replay,
        mechanism_symbols=["core.run"],
        symbol_paths={"core.run": "src/core.py"},
        observable_assertion={
            "source": "stdout",
            "operator": "contains",
            "expected": "shell_probe_failed",
        },
    )
    assert touched == ["core.run"]
    assert link is not None
    assert link["verification_method"] == "runner_harness_observable_dataflow_v1"


def test_atom_binding_uses_structured_snapshot_output_without_ancillary_artifact() -> None:
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:structured"],
        "atom_receipts": [
            {
                "atom_id": "atom:structured",
                "atom_snapshot": {
                    "atom_id": "atom:structured",
                    "command": "python -m tool verify",
                    "exit_code": 3,
                    "text": "The verification command failed.",
                    "output_excerpt": "classifier selected the wrong recovery path",
                },
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "structured-symptom",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:structured"],
                "command": "python -m tool verify",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stderr",
                    "operator": "contains",
                    "expected": "classifier selected the wrong recovery path",
                },
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert errors == []
    assert bindings[0]["match_kind"] == "command_and_atom_evidence_symptom"
    assert bindings[0]["origin_atom_field_path"] == "$.output_excerpt"
    assert "origin_artifact_path" not in bindings[0]


def test_explicit_field_bindings_accept_short_symptom_and_context_atoms() -> None:
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:symptom", "atom:context"],
        "atom_receipts": [
            {
                "atom_id": "atom:symptom",
                "atom_sha256": "1" * 64,
                "atom_snapshot": {
                    "atom_id": "atom:symptom",
                    "command": "python -m product show",
                    "output_excerpt": "bad",
                },
                "artifact_receipts": [],
            },
            {
                "atom_id": "atom:context",
                "atom_sha256": "2" * 64,
                "atom_snapshot": {
                    "atom_id": "atom:context",
                    "platform": "win",
                },
                "artifact_receipts": [],
            },
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "short-wrong-value",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": ["atom:symptom", "atom:context"],
                "command": "python -m product show",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "bad",
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:symptom",
                        "role": "symptom",
                        "field_path": "$.output_excerpt",
                        "value": "bad",
                        "value_sha256": mod._canonical_json_sha256("bad"),
                    },
                    {
                        "atom_id": "atom:context",
                        "role": "context",
                        "field_path": "$.platform",
                        "value": "win",
                    },
                ],
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert errors == []
    assert [binding["binding_role"] for binding in bindings] == [
        "symptom",
        "context",
    ]
    assert bindings[0]["origin_atom_field_path"] == "$.output_excerpt"
    assert bindings[0]["origin_atom_value_sha256"] == mod._canonical_json_sha256("bad")
    assert bindings[1]["origin_atom_sha256"] == "2" * 64


def test_explicit_field_binding_rejects_changed_value_or_unrelated_symptom() -> None:
    assignment = {
        "status": "complete",
        "expected_atom_ids": ["atom:one"],
        "atom_receipts": [
            {
                "atom_id": "atom:one",
                "atom_sha256": "1" * 64,
                "atom_snapshot": {"output_excerpt": "bad"},
                "artifact_receipts": [],
            }
        ],
    }
    dossier = {
        "experiments": [
            {
                "experiment_id": "forged",
                "scenario_kind": "faithful_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "python -m product show",
                "outcome": "supports",
                "observable_assertion": {
                    "source": "stdout",
                    "operator": "equals",
                    "expected": "different",
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:one",
                        "role": "symptom",
                        "field_path": "$.output_excerpt",
                        "value": "bad",
                        "value_sha256": mod._canonical_json_sha256("bad"),
                    }
                ],
            }
        ]
    }
    errors: list[str] = []

    bindings = mod._experiment_atom_bindings(dossier, assignment, errors=errors)

    assert bindings == []
    assert any(error.endswith(":not_bound_to_observation") for error in errors)
    assert "supporting_experiments_do_not_cover_origin_atoms" in errors
    assert "supporting_experiments_have_no_direct_symptom_binding" in errors


def test_declared_mechanism_link_requires_runner_observed_python_call_chain(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "src"
    source.mkdir(parents=True)
    (source / "api.py").write_text(
        "from core import run\ndef execute():\n    return run()\n",
        encoding="utf-8",
    )
    (source / "core.py").write_text("def run():\n    return 'bad'\n", encoding="utf-8")
    experiment = {
        "mechanism_link": {
            "kind": "entrypoint_dataflow",
            "entrypoint": "api.execute",
            "code_path": [
                {
                    "path": "src/api.py",
                    "symbol": "api.execute",
                    "observation": "Calls the result-producing boundary.",
                },
                {
                    "path": "src/core.py",
                    "symbol": "core.run",
                    "observation": "Returns the observed wrong value.",
                },
            ],
        }
    }
    link = mod._verified_declared_mechanism_link(
        experiment=experiment,
        mechanism_symbols=["core.run"],
        symbol_paths={
            "api.execute": "src/api.py",
            "core.run": "src/core.py",
        },
        workspace=workspace,
    )
    assert link is not None
    assert link["verified_call_edges"][0]["resolved_call"] == "core.run"

    (source / "api.py").write_text(
        "def execute():\n    return 'invented nearby explanation'\n",
        encoding="utf-8",
    )
    assert (
        mod._verified_declared_mechanism_link(
            experiment=experiment,
            mechanism_symbols=["core.run"],
            symbol_paths={
                "api.execute": "src/api.py",
                "core.run": "src/core.py",
            },
            workspace=workspace,
        )
        is None
    )
