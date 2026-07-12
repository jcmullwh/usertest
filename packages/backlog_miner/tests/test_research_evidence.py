from __future__ import annotations

import json
import os
import stat
import subprocess
from copy import deepcopy
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


def test_clean_revision_view_reuses_effective_relocated_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    requested = tmp_path / "requested" / "revision-view"
    relocated = tmp_path / "windows-temp" / "revision-view"
    relocated.mkdir(parents=True)
    revision = "a" * 40

    def collide_with_relocated_workspace(**_kwargs: object) -> object:
        raise FileExistsError(relocated)

    monkeypatch.setattr(mod, "acquire_target", collide_with_relocated_workspace)
    monkeypatch.setattr(mod, "_workspace_head", lambda workspace: revision)
    monkeypatch.setattr(mod, "_workspace_clean", lambda workspace: True)

    workspace, head, clean, errors = mod.materialize_clean_revision_view(
        source_workspace=source,
        destination=requested,
        repo_revision=revision,
    )

    assert workspace == relocated.resolve()
    assert head == revision
    assert clean is True
    assert errors == []


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


def _selected_mechanism_binding(
    *,
    hypothesis_id: str,
    mechanism_evidence: list[dict[str, object]],
    causal_root_evidence_ids: list[str],
) -> dict[str, object]:
    code_paths = sorted(
        {
            (str(point["symbol"]), str(point["path"]))
            for evidence in mechanism_evidence
            for point in evidence.get("code_paths", [])
            if isinstance(point, dict) and "symbol" in point and "path" in point
        }
    )
    verified = {
        "schema_version": 3,
        "mechanism_symbols": sorted({symbol for symbol, _path in code_paths}),
        "code_paths": [{"symbol": symbol, "path": path} for symbol, path in code_paths],
    }
    provenance = {
        "schema_version": 2,
        "primary_hypothesis_id": hypothesis_id,
        "mechanism_evidence_ids": sorted(
            str(evidence["mechanism_evidence_id"]) for evidence in mechanism_evidence
        ),
        "causal_root_evidence_ids": sorted(causal_root_evidence_ids),
    }
    return {
        "verified_mechanism": verified,
        "verified_mechanism_sha256": mod._canonical_json_sha256(verified),
        "verified_mechanism_provenance": provenance,
        "verified_mechanism_provenance_sha256": mod._canonical_json_sha256(provenance),
    }


@pytest.mark.parametrize(
    ("outcome", "disproof", "observed", "expected"),
    [
        (
            "disproved",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            True,
        ),
        (
            "disproved",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "not_contains", "expected": "fixed"},
            False,
        ),
        (
            "survived",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "not_contains", "expected": "fixed"},
            True,
        ),
        (
            "survived",
            {"source": "stdout", "operator": "not_contains", "expected": "failure"},
            {"source": "stdout", "operator": "contains", "expected": "failure"},
            True,
        ),
        (
            "survived",
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            {"source": "stdout", "operator": "contains", "expected": "fixed"},
            False,
        ),
        (
            "survived",
            {"source": "exit_code", "operator": "equals", "expected": 0},
            {"source": "exit_code", "operator": "equals", "expected": 1},
            True,
        ),
        (
            "survived",
            {"source": "exit_code", "operator": "equals", "expected": 0},
            {"source": "exit_code", "operator": "equals", "expected": 0},
            False,
        ),
        (
            "inconclusive",
            {"source": "stderr", "operator": "contains", "expected": "x"},
            {"source": "stdout", "operator": "equals", "expected": "y"},
            True,
        ),
    ],
)
def test_falsification_polarity_membership_truth_table_matches_stage_contract(
    outcome: str,
    disproof: dict[str, object],
    observed: dict[str, object],
    expected: bool,
) -> None:
    assert (
        mod._falsification_assertion_relation(
            disproof,
            observed,
            outcome=outcome,
        )
        is expected
    )
    assert (
        stage_contracts._falsification_assertion_relation(
            disproof,
            observed,
            outcome=outcome,
        )
        is expected
    )


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
            "mechanism_symbols": ["core.run"],
        }
    ]
    errors: list[str] = []
    intervention = {
        "hypothesis_id": "h1",
        "attempt_id": "attempt:selected-cause",
        "intervention_receipt_id": "falsification_intervention:verified-delta",
        "shared_verified_mechanism_symbols": ["core.run"],
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
        "command_authorization": mod._command_authorization_receipt(
            {
                "authorization_kind": "standard_test_or_research_harness",
                "executed_argv_sha256": mod._canonical_json_sha256(
                    ["python", ".usertest_research/repro.py"]
                ),
                "shell": False,
                "workspace_confined": True,
                "artifact_id": "artifact:retained-repro",
                "entrypoint_path": ".usertest_research/repro.py",
                "entrypoint_sha256": sha256(harness.read_bytes()).hexdigest(),
            }
        ),
        "exit_code": 1,
        "workspace_dir": str(research),
        "stderr_path": str(stderr_path),
        "stdout_sha256": "a" * 64,
        "stderr_sha256": sha256(stderr_path.read_bytes()).hexdigest(),
        "assertion_passed": True,
    }
    mechanism = {
        "mechanism_evidence_id": "mechanism_evidence:harness",
        "hypothesis_id": "h-harness",
        "evidence_type": "temporary_harness",
        "experiment_ids": ["exp-original"],
        "origin_atom_ids": ["atom:original"],
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
        "harness_path": ".usertest_research/repro.py",
        "adversarial_effect": "supports_selection",
    }
    selected_binding = _selected_mechanism_binding(
        hypothesis_id="h-harness",
        mechanism_evidence=[mechanism],
        causal_root_evidence_ids=["mechanism_evidence:harness"],
    )
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
            "root_cause_hypotheses": [
                {
                    "hypothesis_id": "h-harness",
                    "mechanism_symbols": ["core.run"],
                }
            ],
        },
        clean_replays={"exp-original": replay},
        mechanism_evidence=[mechanism],
        **selected_binding,
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
                "root_cause_hypotheses": [
                    {
                        "hypothesis_id": "h-harness",
                        "mechanism_symbols": ["core.run"],
                    }
                ],
            },
            {
                "outcome_oracles": [oracle],
                "experiments": [experiment],
                "mechanism_evidence": [mechanism],
                **selected_binding,
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
        "hypothesis_id": "h-config",
        "evidence_type": "static_trace",
        "experiment_ids": ["exp-config"],
        "origin_atom_ids": ["atom:config"],
        "mechanism_symbols": ["config:/tool/mode"],
        "code_paths": [{"symbol": "config:/tool/mode", "path": "configs/app.yaml"}],
        "mechanism_link": {
            "verification_method": "runner_deterministic_static_trace_v1",
            "entrypoint": "config:/tool/mode",
        },
        "causal_root_bindings": [
            {
                "kind": "origin_symptom_observation",
                "root_mechanism_symbol": "config:/tool/mode",
            }
        ],
        "adversarial_effect": "supports_selection",
    }
    mechanism["mechanism_evidence_id"] = "mechanism_evidence:" + mod._canonical_json_sha256(
        mechanism
    )
    mechanism_evidence_id = str(mechanism["mechanism_evidence_id"])
    selected_binding = _selected_mechanism_binding(
        hypothesis_id="h-config",
        mechanism_evidence=[mechanism],
        causal_root_evidence_ids=[mechanism_evidence_id],
    )
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
        mechanism_evidence=[mechanism],
    )
    assert len(closure) == 1
    assert closure[0]["closure_basis"] == "rooted_connected_support_component"
    assert closure[0]["support_experiment_ids"] == ["exp-config"]
    assert closure[0]["verification_method"] == ("runner_deterministic_mechanism_closure_v2")
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
        **selected_binding,
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
                "root_cause_hypotheses": dossier["root_cause_hypotheses"],
            },
            {
                "outcome_oracles": [oracle],
                "experiments": [experiment],
                "mechanism_evidence": [mechanism],
                **selected_binding,
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
            "mechanism_evidence": [mechanism],
            **selected_binding,
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


def test_outcome_oracles_ignore_support_from_nonprimary_hypothesis(tmp_path: Path) -> None:
    def experiment(experiment_id: str) -> dict[str, object]:
        return {
            "experiment_id": experiment_id,
            "scenario_kind": "faithful_replay",
            "addresses_atom_ids": ["atom:one"],
            "command": f"python tools/{experiment_id}.py",
            "outcome": "supports",
            "exit_code": 1,
            "observable_assertion": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 1,
            },
            "artifact_refs": ["artifact:one"],
        }

    primary_experiment = experiment("primary-support")
    alternative_experiment = experiment("alternative-support")

    def replay(experiment_id: str) -> dict[str, object]:
        argv = ["python", f"tools/{experiment_id}.py"]
        return {
            "executed_argv": argv,
                "command_authorization": mod._command_authorization_receipt(
                    {
                        "authorization_kind": (
                            "declared_inspected_repository_entrypoint"
                        ),
                        "executed_argv_sha256": mod._canonical_json_sha256(argv),
                        "shell": False,
                        "workspace_confined": True,
                        "entrypoint_path": f"tools/{experiment_id}.py",
                        "entrypoint_sha256": "c" * 64,
                        "entrypoint_git_blob_sha": "d" * 40,
                    }
                ),
            "assertion_passed": True,
            "exit_code": 1,
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        }

    primary_evidence = {
        "mechanism_evidence_id": "mechanism_evidence:primary",
        "hypothesis_id": "h-primary",
        "adversarial_effect": "supports_selection",
        "experiment_ids": ["primary-support"],
        "origin_atom_ids": ["atom:one"],
        "mechanism_symbols": ["core.primary"],
        "code_paths": [{"symbol": "core.primary", "path": "src/core.py"}],
    }
    alternative_evidence = {
        "mechanism_evidence_id": "mechanism_evidence:alternative",
        "hypothesis_id": "h-alternative",
        "adversarial_effect": "supports_selection",
        "experiment_ids": ["alternative-support"],
        "origin_atom_ids": ["atom:one"],
        "mechanism_symbols": ["core.alternative"],
        "code_paths": [{"symbol": "core.alternative", "path": "src/core.py"}],
    }
    selected_binding = _selected_mechanism_binding(
        hypothesis_id="h-primary",
        mechanism_evidence=[primary_evidence],
        causal_root_evidence_ids=["mechanism_evidence:primary"],
    )
    errors: list[str] = []

    oracles = mod._outcome_oracle_receipts(
        {
            "case_id": "case:primary",
            "root_cause_hypotheses": [
                {"hypothesis_id": "h-primary"},
                {"hypothesis_id": "h-alternative"},
            ],
            "experiments": [primary_experiment, alternative_experiment],
        },
        clean_replays={
            "primary-support": replay("primary-support"),
            "alternative-support": replay("alternative-support"),
        },
        mechanism_evidence=[primary_evidence, alternative_evidence],
        **selected_binding,
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment={},
        atom_bindings=[],
        planning_workspace=None,
        research_workspace=None,
        overlay_manifest={},
        run_dir=tmp_path / "runs" / "usertest" / "primary-only",
        repo_revision="a" * 40,
        errors=errors,
    )

    assert errors == []
    assert [oracle["research_experiment_id"] for oracle in oracles] == ["primary-support"]
    assert oracles[0]["primary_hypothesis_id"] == "h-primary"
    assert oracles[0]["mechanism_evidence_ids"] == ["mechanism_evidence:primary"]


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
                    "adversarial_effect": "supports_selection",
                    "origin_symptom_bindings": [
                        {
                            "experiment_id": "support",
                            "atom_id": "atom:one",
                            "match_kind": "command_and_atom_evidence_symptom",
                            "origin_atom_sha256": "a" * 64,
                        }
                    ],
                    "origin_atom_ids": ["atom:one"],
                    "experiment_ids": ["support"],
                    "mechanism_link": {
                        "entrypoint": "router.route",
                        "verification_method": "runner_exception_symbol_trace_v1",
                    },
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
            "schema_version": 3,
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
        "inspected_files": ["tests/test_core.py"],
        "experiments": [
            {
                "experiment_id": "claimed-failure",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:one"],
                "command": "pytest -q tests/test_core.py -k guarded_control",
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
        "artifact_refs": [
            {
                "artifact_id": "artifact:overlay-harness",
                "path": ".usertest_research/test_repro.py",
            }
        ],
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
        "inspected_files": ["tests/test_mutation.py"],
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


def test_preexisting_repository_cli_is_hash_bound_without_language_whitelist(
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

    assert errors == []
    assert receipts["unbound"]["command_authorization"]["authorization_kind"] == (
        "immutable_repository_entrypoint"
    )
    assert receipts["unbound"]["command_authorization"]["entrypoint_path"] == (
        "tools/show_mode.py"
    )
    assert receipts["unbound"]["command_authorization"]["entrypoint_git_blob_sha"]


@pytest.mark.parametrize(
    ("manifest_name", "manifest_content", "command"),
    [
        ("Cargo.toml", "[package]\nname='depth-test'\nversion='0.1.0'\n", "cargo test"),
        ("go.mod", "module example.invalid/depth\n\ngo 1.23\n", "go test ./..."),
        ("pom.xml", "<project><modelVersion>4.0.0</modelVersion></project>\n", "mvn test"),
        ("build.gradle", "plugins { id 'java' }\n", "gradle test"),
        ("Depth.Tests.csproj", "<Project Sdk=\"Microsoft.NET.Sdk\" />\n", "dotnet test"),
    ],
)
def test_repository_native_runner_authorization_uses_declared_tracked_bindings(
    tmp_path: Path,
    manifest_name: str,
    manifest_content: str,
    command: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / manifest_name).write_text(manifest_content, encoding="utf-8")
    _baseline_repo_commit_existing(workspace, f"add {manifest_name}")
    experiment = {
        "scenario_kind": "original_replay",
        "repository_bindings": [
            {
                "path": manifest_name,
                "relationship": "Tracked project manifest governing this exact runner command.",
            }
        ],
    }
    dossier = {"inspected_files": [manifest_name]}

    authorized = mod._authorized_replay_invocation(
        command=command,
        experiment=experiment,
        dossier=dossier,
        assignment={},
        workspace=workspace,
    )

    assert authorized is not None
    argv, receipt = authorized
    assert receipt["authorization_kind"] == "declared_repository_bindings"
    assert receipt["repository_bindings"][0]["path"] == manifest_name
    assert receipt["repository_bindings"][0]["git_blob_sha"]
    assert mod._command_authorization_attested(receipt, argv=argv)


def test_declared_repository_binding_cannot_bypass_inspection_or_tracking(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    _baseline_repo_commit_existing(workspace, "tracked manifest")
    experiment = {
        "scenario_kind": "original_replay",
        "repository_bindings": [
            {"path": "Cargo.toml", "relationship": "governs the workspace"}
        ],
    }

    assert mod._authorized_replay_invocation(
        command="cargo test",
        experiment=experiment,
        dossier={"inspected_files": []},
        assignment={},
        workspace=workspace,
    ) is None


def test_git_attestation_helpers_do_not_impose_convenience_timeouts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    def run(_argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls.append(dict(kwargs))
        output = "a" * 40 + "\n"
        return subprocess.CompletedProcess(
            _argv,
            0,
            stdout=output if kwargs.get("text") is True else output.encode("utf-8"),
            stderr="" if kwargs.get("text") is True else b"",
        )

    monkeypatch.setattr(mod.subprocess, "run", run)

    assert mod._workspace_head(tmp_path) == "a" * 40
    assert mod._workspace_clean(tmp_path) is False
    assert mod._git_output_bytes(tmp_path, "status") == ("a" * 40 + "\n").encode()
    assert mod._git_blob_sha(tmp_path, "Cargo.toml") == "a" * 40
    assert calls
    assert all("timeout" not in kwargs for kwargs in calls)


def _runner_bound_atom_assignment(
    *, atom_id: str, atom_snapshot: dict[str, object]
) -> dict[str, object]:
    receipt = {
        "atom_id": atom_id,
        "atom_sha256": mod._canonical_json_sha256(atom_snapshot),
        "atom_snapshot": atom_snapshot,
    }
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "expected_atom_ids": [atom_id],
        "atom_receipts": [receipt],
    }
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)
    return assignment


def test_powershell_environment_adapter_runs_through_production_replay_and_oracle(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    probe = baseline / "tools" / "environment_probe.ps1"
    probe.parent.mkdir()
    probe.write_text(
        "@{ mode = $env:BACKLOG_DEPTH_TEST_MODE } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(baseline, "powershell environment probe")
    atom_id = "atom:powershell-environment"
    command = "powershell.exe -NoProfile -File tools/environment_probe.ps1"
    assignment = _runner_bound_atom_assignment(
        atom_id=atom_id,
        atom_snapshot={
            "command": command,
            "exit_code": 0,
            "expected_mode": "ready",
            "text": "The signed-in host path should report ready when the mode is supplied.",
            "evidence_role": "observation",
            "origin_stage": "runtime",
        },
    )
    claim = {
        "adapter_id": "environment.v1",
        "hypothesis_id": "hypothesis:environment",
        "baseline_experiment_id": "experiment:without-mode",
        "challenge_experiment_id": "experiment:with-mode",
        "intervention": {
            "kind": "child_environment_variable",
            "target": "env:BACKLOG_DEPTH_TEST_MODE",
            "predicted_polarity": "missing_to_present",
            "before": None,
            "after": "ready",
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/mode"},
            "challenge": {"source": "stdout_json", "json_pointer": "/mode"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": "ready"},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_mode",
            },
        },
        "implementation_touchpoints": [
            {
                "causal_locator": "env:BACKLOG_DEPTH_TEST_MODE",
                "path": "tools/environment_probe.ps1",
                "symbols": [],
                "relationship": (
                    "This inspected production entrypoint reads the controlled child variable."
                ),
            }
        ],
    }
    experiments = [
        {
            "experiment_id": "experiment:without-mode",
            "scenario_kind": "runtime_environment_absent",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The child process reports no mode.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_TEST_MODE": None}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '"mode":null',
            },
            "verification_boundary": {
                "boundary_kind": "isolated_child_environment_equivalence",
                "requires_live_verification": False,
                "faithful_equivalence": True,
                "rationale": (
                    "The isolated replay executes the same tracked entrypoint with the exact "
                    "controlled child input and evaluates the original positive predicate."
                ),
            },
            "artifact_refs": [],
        },
        {
            "experiment_id": "experiment:with-mode",
            "scenario_kind": "runtime_environment_present",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The child process reports the controlled mode.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_TEST_MODE": "ready"}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '"mode":"ready"',
            },
            "artifact_refs": [],
            "proof_adapter": claim,
        },
    ]
    dossier: dict[str, object] = {
        "case_id": "case:powershell-environment",
        "problem_id": "problem:powershell-environment",
        "repo_revision": revision,
        "experiments": experiments,
        "inspected_files": ["tools/environment_probe.ps1"],
        "artifact_refs": [],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:environment",
                "statement": "The child environment controls the observed mode.",
                "mechanism_symbols": ["env:BACKLOG_DEPTH_TEST_MODE"],
                "supporting_evidence": [
                    "experiment:without-mode",
                    "experiment:with-mode",
                ],
                "counterevidence": [],
                "falsification_attempts": [],
                "disposition": "primary",
            }
        ],
    }
    replay_errors: list[str] = []
    replays = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=None,
        errors=replay_errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )
    assert replay_errors == []
    assert set(replays) == {"experiment:without-mode", "experiment:with-mode"}
    assert all(
        replay["command_authorization"]["authorization_kind"]
        in {"immutable_source_command", "immutable_repository_entrypoint"}
        for replay in replays.values()
    )
    atom_receipt = assignment["atom_receipts"][0]
    atom_bindings = [
        {
            "experiment_id": experiment_id,
            "atom_id": atom_id,
            "match_kind": "adapter_declared_symptom",
            "origin_atom_sha256": atom_receipt["atom_sha256"],
        }
        for experiment_id in replays
    ]
    experiment_index = {
        str(experiment["experiment_id"]): experiment for experiment in experiments
    }
    probe_sha256 = mod.sha256(probe.read_bytes()).hexdigest()
    inspected_file_receipts = [
        {
            "path": "tools/environment_probe.ps1",
            "sha256": probe_sha256,
            "whole_file_observed": True,
            "observed_content_sha256": probe_sha256,
        }
    ]
    proofs, diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments=experiment_index,
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
        inspected_file_receipts=inspected_file_receipts,
    )
    assert diagnostics == []
    assert len(proofs) == 1
    assert proofs[0]["adapter_id"] == "environment.v1"
    assert proofs[0]["positive_outcome"]["passed"] is True
    touchpoint = proofs[0]["adapter_evidence"]["implementation_touchpoints"][0]
    assert touchpoint == {
        "touchpoint_id": f"implementation_touchpoint:{touchpoint['evidence_sha256']}",
        "causal_locator": "env:BACKLOG_DEPTH_TEST_MODE",
        "path": "tools/environment_probe.ps1",
        "symbols": [],
        "relationship": (
            "This inspected production entrypoint reads the controlled child variable."
        ),
        "runner_attested": True,
        "inspected_content_sha256": probe_sha256,
        "evidence_sha256": touchpoint["evidence_sha256"],
    }
    first_consumer = mod._adapter_executed_consumer_receipt(
        proofs[0],
        clean_replays=replays,
        implementation_touchpoints=[touchpoint],
    )
    repeated_consumer = mod._adapter_executed_consumer_receipt(
        proofs[0],
        clean_replays=replays,
        implementation_touchpoints=[touchpoint],
    )
    assert first_consumer == repeated_consumer

    second_replays = deepcopy(replays)
    for replay in second_replays.values():
        argv = [
            (
                "tools/environment_probe_secondary.ps1"
                if argument == "tools/environment_probe.ps1"
                else argument
            )
            for argument in replay["executed_argv"]
        ]
        authorization = replay["command_authorization"]
        replay["executed_argv"] = argv
        replay["command_authorization"] = mod._command_authorization_receipt(
            {
                **{
                    key: value
                    for key, value in authorization.items()
                    if key not in {"authorization_sha256", "runner_attested"}
                },
                "executed_argv_sha256": mod._canonical_json_sha256(argv),
                "entrypoint_path": "tools/environment_probe_secondary.ps1",
                "entrypoint_sha256": "6" * 64,
                "entrypoint_git_blob_sha": "7" * 40,
            }
        )
    second_touchpoint_projection = {
        key: value
        for key, value in touchpoint.items()
        if key not in {"touchpoint_id", "evidence_sha256"}
    }
    second_touchpoint_projection["path"] = "tools/environment_probe_secondary.ps1"
    second_touchpoint_projection["inspected_content_sha256"] = "6" * 64
    second_touchpoint_hash = mod._canonical_json_sha256(second_touchpoint_projection)
    second_touchpoint = {
        "touchpoint_id": f"implementation_touchpoint:{second_touchpoint_hash}",
        **second_touchpoint_projection,
        "evidence_sha256": second_touchpoint_hash,
    }
    second_consumer = mod._adapter_executed_consumer_receipt(
        proofs[0],
        clean_replays=second_replays,
        implementation_touchpoints=[second_touchpoint],
    )
    assert first_consumer is not None
    assert second_consumer is not None
    assert first_consumer["causal_target"] == second_consumer["causal_target"]
    assert first_consumer["consumer_identity"] != second_consumer["consumer_identity"]
    proofs_with_ancillary, ancillary_diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments={
            **experiment_index,
            "experiment:ancillary-invalid": {
                "experiment_id": "experiment:ancillary-invalid",
                "proof_adapter": {**claim, "adapter_id": "vendor.unregistered.v1"},
            },
        },
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
        inspected_file_receipts=inspected_file_receipts,
    )
    assert proofs_with_ancillary == proofs
    assert ancillary_diagnostics == [
        {
            "experiment_id": "experiment:ancillary-invalid",
            "adapter_id": "vendor.unregistered.v1",
            "claim_sha256": mod._canonical_json_sha256(
                {**claim, "adapter_id": "vendor.unregistered.v1"}
            ),
            "diagnostics": ["proof_adapter_unavailable:vendor.unregistered.v1"],
        }
    ]
    invalid_touchpoint_claim = {
        **claim,
        "implementation_touchpoints": [
            {
                "causal_locator": "env:BACKLOG_DEPTH_TEST_MODE",
                "path": ".usertest_research/probe.ps1",
                "symbols": [],
                "relationship": "A research harness is not a production change surface.",
            }
        ],
    }
    invalid_proofs, invalid_touchpoint_diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments={
            **experiment_index,
            "experiment:with-mode": {
                **experiment_index["experiment:with-mode"],
                "proof_adapter": invalid_touchpoint_claim,
            },
        },
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
        inspected_file_receipts=inspected_file_receipts,
    )
    assert len(invalid_proofs) == 1
    assert "implementation_touchpoints" not in invalid_proofs[0]["adapter_evidence"]
    assert invalid_touchpoint_diagnostics[0]["diagnostics"] == [
        "proof_adapter_implementation_touchpoint_invalid:0"
    ]
    mechanism_errors: list[str] = []
    mechanism = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=[],
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        proof_adapter_receipts=proofs,
        atom_bindings=atom_bindings,
        errors=mechanism_errors,
    )
    assert mechanism_errors == []
    assert len(mechanism) == 1
    assert mechanism[0]["implementation_touchpoints"] == [touchpoint]
    selected = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=mechanism,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert selected[0] is not None
    oracle_errors: list[str] = []
    oracles = mod._outcome_oracle_receipts(
        dossier,
        clean_replays=replays,
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        verified_mechanism=selected[0],
        verified_mechanism_sha256=selected[1],
        verified_mechanism_provenance=selected[2],
        verified_mechanism_provenance_sha256=selected[3],
        control_verifications=[],
        falsification_interventions=[],
        inspected_file_receipts=[],
        inspected_symbol_receipts=[],
        evidence_assignment=assignment,
        atom_bindings=atom_bindings,
        planning_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        run_dir=tmp_path / "runs" / "environment",
        repo_revision=revision,
        errors=oracle_errors,
    )
    assert oracle_errors == []
    assert len(oracles) == 1
    assert oracles[0]["kind"] == "causal_proof_replay"
    assert oracles[0]["execution"]["replay_inputs"] == proofs[0]["replay_inputs"]
    assert oracles[0]["execution"]["replay_observation"] == proofs[0][
        "replay_observation"
    ]
    contract = oracles[0]["positive_outcome_contracts"][0]
    assert contract["kind"] == "causal_proof_predicate"
    assert contract["postconditions"][0]["predicate"] == {
        "kind": "equals",
        "expected": "ready",
    }
    boundary_errors: list[str] = []
    boundaries, boundary_errors = mod._verification_boundary_receipts(
        experiments=experiment_index,
        clean_replays=replays,
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        outcome_oracles=oracles,
        verified_mechanism_provenance=selected[2],
    )
    assert boundary_errors == []
    assert len(boundaries) == 1
    assert boundaries[0]["experiment_id"] == "experiment:without-mode"
    assert boundaries[0]["requires_live_verification"] is False
    assert boundaries[0]["faithful_equivalence"] is True
    equivalence = boundaries[0]["equivalence_proof"]
    assert equivalence["source_experiment_id"] == "experiment:without-mode"
    assert equivalence["origin_atom_ids"] == [atom_id]
    assert equivalence["proof_receipt_id"] == proofs[0]["proof_receipt_id"]
    assert equivalence["replay_inputs_sha256"] == proofs[0]["replay_inputs"][
        "replay_inputs_sha256"
    ]
    assert equivalence["replay_observation_sha256"] == proofs[0][
        "replay_observation"
    ]["replay_observation_sha256"]
    assert mechanism[0]["mechanism_evidence_id"] in boundaries[0]["provenance_refs"]
    assert oracles[0]["outcome_oracle_id"] in boundaries[0]["provenance_refs"]
    assert boundaries[0]["boundary_sha256"] == mod._canonical_json_sha256(
        {
            key: value
            for key, value in boundaries[0].items()
            if key != "boundary_sha256"
        }
    )
    rejected, rejected_errors = mod._verification_boundary_receipts(
        experiments=experiment_index,
        clean_replays=replays,
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        outcome_oracles=[],
        verified_mechanism_provenance=selected[2],
    )
    assert rejected == []
    assert rejected_errors == [
        "verification_boundary_invalid:experiment:without-mode:"
        "faithful_equivalence_unattested"
    ]
    source_replay = dict(replays["experiment:without-mode"])
    source_authorization = source_replay["command_authorization"]
    source_replay["command_authorization"] = mod._command_authorization_receipt(
        {
            key: value
            for key, value in source_authorization.items()
            if key
            not in {
                "authorization_sha256",
                "runner_attested",
                "origin_atom_id",
                "origin_atom_sha256",
                "origin_atom_field_path",
                "origin_command_value_sha256",
            }
        }
    )
    no_identity, no_identity_errors = mod._verification_boundary_receipts(
        experiments=experiment_index,
        clean_replays={**replays, "experiment:without-mode": source_replay},
        mechanism_evidence=mechanism,
        proof_adapter_receipts=proofs,
        outcome_oracles=oracles,
        verified_mechanism_provenance=selected[2],
    )
    assert no_identity == []
    assert no_identity_errors == [
        "verification_boundary_invalid:experiment:without-mode:"
        "faithful_equivalence_unattested"
    ]
    verification = {
        "experiments": list(replays.values()),
        "mechanism_evidence": mechanism,
        "proof_adapter_receipts": proofs,
        "verified_mechanism": selected[0],
        "verified_mechanism_sha256": selected[1],
        "verified_mechanism_provenance": selected[2],
        "verified_mechanism_provenance_sha256": selected[3],
        "outcome_oracles": oracles,
        "inspected_files": [],
        "inspected_symbols": [],
        "falsification_interventions": [],
        "atom_bindings": atom_bindings,
    }
    dossier["evidence_assignment"] = assignment
    assert stage_contracts._validate_outcome_oracles(
        dossier,
        verification,
        pid=str(dossier["problem_id"]),
    ) == []


def test_filesystem_adapter_attests_disposable_state_without_tracked_mutation(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    probe = baseline / "tools" / "state_probe.ps1"
    probe.parent.mkdir()
    probe.write_text(
        "param([string]$Mode)\n"
        "$path = Join-Path (Get-Location) 'tmp/state.json'\n"
        "if ($Mode -eq 'create') {\n"
        "  New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null\n"
        "  '{\"ready\":true}' | Set-Content -NoNewline $path\n"
        "}\n"
        "if (Test-Path $path) { 'present' } else { 'absent' }\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(baseline, "powershell state probe")
    atom_id = "atom:filesystem-state"
    baseline_command = "powershell.exe -NoProfile -File tools/state_probe.ps1 -Mode absent"
    challenge_command = "powershell.exe -NoProfile -File tools/state_probe.ps1 -Mode create"
    assignment = _runner_bound_atom_assignment(
        atom_id=atom_id,
        atom_snapshot={
            "command": baseline_command,
            "expected_exists": True,
            "text": "The original scenario requires the state artifact to be created.",
            "evidence_role": "observation",
            "origin_stage": "runtime",
        },
    )
    claim = {
        "adapter_id": "filesystem_state.v1",
        "hypothesis_id": "hypothesis:filesystem",
        "baseline_experiment_id": "experiment:absent",
        "challenge_experiment_id": "experiment:created",
        "intervention": {
            "kind": "disposable_workspace_state",
            "target": "fs:tmp/state.json",
            "predicted_polarity": "absent_to_present",
            "before": False,
            "after": True,
        },
        "state_inputs": {
            "observation_kind": "existence",
            "baseline_path": "tmp/state.json",
            "challenge_path": "tmp/state.json",
        },
        "positive_outcome": {
            "predicate": {"kind": "existence", "expected": True},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_exists",
            },
        },
    }
    experiments = [
        {
            "experiment_id": "experiment:absent",
            "scenario_kind": "disposable_state_absent",
            "addresses_atom_ids": [atom_id],
            "command": baseline_command,
            "result": "The state file is absent.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"disposable_state_paths": ["tmp/state.json"]},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": "absent",
            },
            "artifact_refs": [],
        },
        {
            "experiment_id": "experiment:created",
            "scenario_kind": "disposable_state_created",
            "addresses_atom_ids": [atom_id],
            "command": challenge_command,
            "result": "The state file is created.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"disposable_state_paths": ["tmp/state.json"]},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": "present",
            },
            "artifact_refs": [],
            "proof_adapter": claim,
        },
    ]
    dossier = {
        "case_id": "case:filesystem-state",
        "problem_id": "problem:filesystem-state",
        "experiments": experiments,
        "artifact_refs": [],
        "inspected_files": [],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:filesystem",
                "mechanism_symbols": ["fs:tmp/state.json"],
            }
        ],
    }
    errors: list[str] = []
    replays = mod._clean_replay_receipts(
        dossier,
        evidence_assignment=assignment,
        baseline_workspace=baseline,
        research_workspace=baseline,
        overlay_manifest={},
        replay_root=tmp_path / "replays",
        repo_revision=revision,
        timeout_seconds=None,
        errors=errors,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=baseline,
        ),
    )
    assert errors == []
    assert replays["experiment:absent"]["post_replay_mutations"] is False
    assert replays["experiment:created"]["post_replay_mutations"] is True
    assert replays["experiment:created"]["undeclared_post_replay_mutations"] == []
    assert replays["experiment:created"]["declared_state_transitions"]
    atom_receipt = assignment["atom_receipts"][0]
    bindings = [
        {
            "experiment_id": experiment_id,
            "atom_id": atom_id,
            "match_kind": "adapter_declared_symptom",
            "origin_atom_sha256": atom_receipt["atom_sha256"],
        }
        for experiment_id in replays
    ]
    proofs, diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        experiments={str(item["experiment_id"]): item for item in experiments},
        clean_replays=replays,
        evidence_assignment=assignment,
        atom_bindings=bindings,
        planning_workspace=baseline,
        symbol_receipts=[],
        artifact_receipts=[],
    )
    assert diagnostics == []
    assert len(proofs) == 1
    assert proofs[0]["adapter_id"] == "filesystem_state.v1"
    assert proofs[0]["positive_outcome"]["observed"] == {"exists": True}


def test_exact_origin_scenario_can_attest_equivalence_without_redundant_adapter() -> None:
    experiment_id = "experiment:exact-original"
    atom_id = "atom:exact-original"
    argv = ["pytest", "tests/test_exact.py::test_original"]
    authorization = mod._command_authorization_receipt(
        {
            "authorization_kind": "immutable_source_command",
            "executed_argv_sha256": mod._canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            "origin_atom_id": atom_id,
            "origin_atom_sha256": "a" * 64,
            "origin_atom_field_path": "$.command",
            "origin_command_value_sha256": "b" * 64,
        }
    )
    replay_inputs = mod._replay_inputs_receipt(
        source_experiment_id=experiment_id,
        environment_overrides={},
        disposable_state_paths=[],
    )
    replay = {
        "experiment_id": experiment_id,
        "executed_argv": argv,
        "command_authorization": authorization,
        "assertion_passed": True,
        "exit_code": 1,
        "stdout_sha256": "c" * 64,
        "stderr_sha256": "d" * 64,
        "execution_isolation": {"executor": "docker", "network": "none"},
        "replay_inputs": replay_inputs,
    }
    contract = {
        "schema_version": 1,
        "kind": "repository_test_assertion",
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    contract["positive_outcome_contract_id"] = mod._content_addressed_receipt_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    replay_observation = mod._exact_original_replay_observation(
        experiment_id=experiment_id,
        replay=replay,
        positive_outcome_contracts=[contract],
    )
    assert replay_observation is not None
    oracle = {
        "schema_version": 1,
        "research_experiment_id": experiment_id,
        "kind": "staged_replay",
        "execution": {
            "argv": argv,
            "command_authorization": authorization,
            "replay_inputs": replay_inputs,
            "replay_observation": replay_observation,
        },
        "positive_outcome_contracts": [contract],
    }
    oracle["outcome_oracle_id"] = mod._content_addressed_receipt_id(
        "outcome_oracle",
        oracle,
        "outcome_oracle_id",
    )
    mechanism_id = "mechanism_evidence:exact-original"
    mechanism = {
        "mechanism_evidence_id": mechanism_id,
        "experiment_ids": [experiment_id],
        "origin_atom_ids": [atom_id],
    }
    experiments = {
        experiment_id: {
            "experiment_id": experiment_id,
            "verification_boundary": {
                "boundary_kind": "repository_original_scenario",
                "requires_live_verification": False,
                "faithful_equivalence": True,
                "rationale": "The exact source command is the original local scenario.",
            },
        }
    }

    boundaries, errors = mod._verification_boundary_receipts(
        experiments=experiments,
        clean_replays={experiment_id: replay},
        mechanism_evidence=[mechanism],
        proof_adapter_receipts=[],
        outcome_oracles=[oracle],
        verified_mechanism_provenance={"mechanism_evidence_ids": [mechanism_id]},
    )

    assert errors == []
    assert len(boundaries) == 1
    equivalence = boundaries[0]["equivalence_proof"]
    assert equivalence["equivalence_mode"] == "exact_origin_scenario_identity"
    assert equivalence["source_identity"]["origin_atom_id"] == atom_id
    assert equivalence["positive_outcome_contract_ids"] == [
        contract["positive_outcome_contract_id"]
    ]

    tampered_observation = dict(replay_observation)
    tampered_observation["selector"] = {"source": "stderr_text"}
    tampered_oracle = {
        **oracle,
        "execution": {**oracle["execution"], "replay_observation": tampered_observation},
    }
    rejected, rejected_errors = mod._verification_boundary_receipts(
        experiments=experiments,
        clean_replays={experiment_id: replay},
        mechanism_evidence=[mechanism],
        proof_adapter_receipts=[],
        outcome_oracles=[tampered_oracle],
        verified_mechanism_provenance={"mechanism_evidence_ids": [mechanism_id]},
    )
    assert rejected == []
    assert rejected_errors == [
        f"verification_boundary_invalid:{experiment_id}:faithful_equivalence_unattested"
    ]


def test_top_level_verifier_dispatches_powershell_adapter_and_persists_proof(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = workspace / "tools" / "environment_probe.ps1"
    probe.parent.mkdir()
    probe.write_text(
        "@{ mode = $env:BACKLOG_DEPTH_VERIFY_MODE } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    revision = _baseline_repo_commit_existing(workspace, "top-level powershell proof")
    command = "powershell.exe -NoProfile -File tools/environment_probe.ps1"
    atom_id = "atom:top-level-powershell"
    atom = {
        "atom_id": atom_id,
        "command": command,
        "exit_code": 0,
        "expected_mode": "ready",
        "text": (
            'The observed states are {"mode":null} and {"mode":"ready"}; '
            "the required mode is ready."
        ),
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    assignment = _runner_bound_atom_assignment(atom_id=atom_id, atom_snapshot=atom)
    assignment.update(
        case_id="case:top-level-powershell",
        problem_id="problem:top-level-powershell",
    )
    assignment["atom_receipts"][0]["origin_evidence_mode"] = "signed_snapshot"
    assignment["assignment_sha256"] = mod.evidence_assignment_sha256(assignment)
    claim = {
        "adapter_id": "environment.v1",
        "hypothesis_id": "hypothesis:top-level-environment",
        "baseline_experiment_id": "experiment:mode-absent",
        "challenge_experiment_id": "experiment:mode-ready",
        "intervention": {
            "kind": "child_environment_variable",
            "target": "env:BACKLOG_DEPTH_VERIFY_MODE",
            "predicted_polarity": "absent_to_ready",
            "before": None,
            "after": "ready",
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/mode"},
            "challenge": {"source": "stdout_json", "json_pointer": "/mode"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": "ready"},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_mode",
            },
        },
        "implementation_touchpoints": [
            {
                "causal_locator": "env:BACKLOG_DEPTH_VERIFY_MODE",
                "path": "tools/environment_probe.ps1",
                "symbols": [],
                "relationship": (
                    "This inspected production entrypoint consumes the controlled mode."
                ),
            }
        ],
    }
    experiments = [
        {
            "experiment_id": "experiment:mode-absent",
            "scenario_kind": "environment_mode_absent",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The clean child reports a null mode.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_VERIFY_MODE": None}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '{"mode":null}',
            },
            "verification_boundary": {
                "boundary_kind": "isolated_child_environment_equivalence",
                "requires_live_verification": False,
                "faithful_equivalence": True,
                "rationale": (
                    "The isolated replay preserves the tracked entrypoint, controlled input, "
                    "and runner-evaluated positive predicate."
                ),
            },
            "artifact_refs": [],
        },
        {
            "experiment_id": "experiment:mode-ready",
            "scenario_kind": "environment_mode_ready",
            "addresses_atom_ids": [atom_id],
            "command": command,
            "result": "The controlled child reports ready.",
            "outcome": "supports",
            "exit_code": 0,
            "replay_setup": {"environment": {"BACKLOG_DEPTH_VERIFY_MODE": "ready"}},
            "observable_assertion": {
                "source": "stdout",
                "operator": "contains",
                "expected": '{"mode":"ready"}',
            },
            "artifact_refs": [],
            "proof_adapter": claim,
        },
    ]
    statement = "The child environment value controls the emitted mode."
    dossier: dict[str, object] = {
        "research_schema_version": 3,
        "case_id": "case:top-level-powershell",
        "problem_id": "problem:top-level-powershell",
        "repo_revision": revision,
        "research_method": "runner_registered_environment_adapter",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "writes_used": False,
        "writes_purpose": ["none"],
        "implementation_performed": False,
        "diff_classification": "no_changes",
        "artifact_refs": [],
        "experiments": experiments,
        "inspected_files": ["tools/environment_probe.ps1"],
        "inspected_symbols": [],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:top-level-environment",
                "statement": statement,
                "supporting_evidence": [
                    "experiment:mode-absent",
                    "experiment:mode-ready",
                ],
                "counterevidence": [],
                "mechanism_symbols": ["env:BACKLOG_DEPTH_VERIFY_MODE"],
                "disposition": "primary",
                "disposition_evidence": ["experiment:mode-ready"],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:environment-does-not-control-output",
                        "hypothesis_id": "hypothesis:top-level-environment",
                        "claim": statement,
                        "baseline_experiment_id": "experiment:mode-absent",
                        "challenge_experiment_id": "experiment:mode-ready",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "not_contains",
                            "expected": '{"mode":"ready"}',
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "root_cause_confidence": 0.51,
        "broader_class_assessment": "unknown",
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [],
        "evidence_assignment": assignment,
    }
    run_dir = tmp_path / "research-run"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps({"schema_version": 1, "kind": "troubleshoot_v1", "status": "success"}),
        encoding="utf-8",
    )
    (run_dir / "workspace_ref.json").write_text(
        json.dumps({"workspace_dir": str(workspace)}),
        encoding="utf-8",
    )
    (run_dir / "target_ref.json").write_text(
        json.dumps({"ref": revision, "commit_sha": revision, "agent": "claude"}),
        encoding="utf-8",
    )
    dossier["artifact_refs"] = [
        {
            "artifact_id": "runner:target_ref",
            "kind": "runner_provenance",
            "path": str(run_dir / "target_ref.json"),
        }
    ]
    observed_probe = probe.read_text(encoding="utf-8")
    events = [
        {"type": "run_command", "data": {"command": command, "exit_code": 0}},
        {"type": "run_command", "data": {"command": command, "exit_code": 0}},
        {
            "type": "read_file",
            "data": {
                "path": "tools/environment_probe.ps1",
                "bytes": probe.stat().st_size,
                "read_source": "tool",
                "source_exit_code": 0,
                **observed_read_attestation(
                    path=probe,
                    observed_text=observed_probe,
                    source_exit_code=0,
                    allow_partial=False,
                ),
            },
        },
    ]
    (run_dir / "normalized_events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    unverified_dossier = deepcopy(dossier)
    receipt = mod.verify_research_evidence(
        dossier,
        run_dir=run_dir,
        repo_revision=revision,
        case_id=str(dossier["case_id"]),
        problem_id=str(dossier["problem_id"]),
        expected_case_id=str(dossier["case_id"]),
        expected_problem_id=str(dossier["problem_id"]),
        evidence_assignment=assignment,
        evidence_atom_ids=[atom_id],
        revision_view_destination=tmp_path / "revision-view",
        replay_timeout_seconds=None,
        requested_repo_ref=revision,
        resolved_repo_ref=revision,
        replay_executor=mod.TrustedHostReplayExecutor(
            approved_source_roots=[tmp_path],
            source_identity=workspace,
        ),
    )
    dossier["evidence_verification"] = receipt

    assert receipt["status"] == "verified", receipt["errors"]
    assert len(receipt["proof_adapter_receipts"]) == 1
    assert receipt["proof_adapter_receipts"][0]["adapter_id"] == "environment.v1"
    assert len(receipt["outcome_oracles"]) == 1
    assert receipt["outcome_oracles"][0]["kind"] == "causal_proof_replay"
    assert len(receipt["verification_boundaries"]) == 1
    assert receipt["verification_boundaries"][0]["requires_live_verification"] is False
    assert receipt["quarantined_diagnostics"]
    assert receipt["verified_mechanism"] is not None
    adapter_mechanism = receipt["mechanism_evidence"][0]
    assert adapter_mechanism["causal_target"] == "env:BACKLOG_DEPTH_VERIFY_MODE"
    assert adapter_mechanism["consumer_identity"]["runner_attested"] is True
    assert adapter_mechanism["consumer_identity"]["entrypoint"] == (
        "tools/environment_probe.ps1"
    )
    assert adapter_mechanism["executed_consumer"]["causal_target"] == (
        "env:BACKLOG_DEPTH_VERIFY_MODE"
    )
    persisted_valid, persisted_errors = mod.verify_persisted_research_evidence(dossier)
    assert persisted_errors == []
    assert persisted_valid is True
    ready, readiness_reasons = stage_contracts.assess_research_readiness(dossier)
    assert ready is True, "\n".join(readiness_reasons)

    for mode in ("missing", "unconnected"):
        rejected_dossier = deepcopy(unverified_dossier)
        rejected_claim = rejected_dossier["experiments"][1]["proof_adapter"]
        if mode == "missing":
            rejected_claim.pop("implementation_touchpoints")
        else:
            rejected_claim["implementation_touchpoints"][0]["causal_locator"] = (
                "unrelated:mode"
            )
        rejected_run_dir = tmp_path / f"research-run-{mode}"
        rejected_run_dir.mkdir()
        for filename in (
            "report.json",
            "workspace_ref.json",
            "target_ref.json",
            "normalized_events.jsonl",
        ):
            (rejected_run_dir / filename).write_bytes((run_dir / filename).read_bytes())
        rejected_dossier["artifact_refs"][0]["path"] = str(
            rejected_run_dir / "target_ref.json"
        )
        rejected_receipt = mod.verify_research_evidence(
            rejected_dossier,
            run_dir=rejected_run_dir,
            repo_revision=revision,
            case_id=str(rejected_dossier["case_id"]),
            problem_id=str(rejected_dossier["problem_id"]),
            expected_case_id=str(rejected_dossier["case_id"]),
            expected_problem_id=str(rejected_dossier["problem_id"]),
            evidence_assignment=assignment,
            evidence_atom_ids=[atom_id],
            revision_view_destination=tmp_path / f"revision-view-{mode}",
            replay_timeout_seconds=None,
            requested_repo_ref=revision,
            resolved_repo_ref=revision,
            replay_executor=mod.TrustedHostReplayExecutor(
                approved_source_roots=[tmp_path],
                source_identity=workspace,
            ),
        )
        rejected_dossier["evidence_verification"] = rejected_receipt
        assert rejected_receipt["status"] == "verified", rejected_receipt["errors"]
        rejected_ready, rejected_reasons = stage_contracts.assess_research_readiness(
            rejected_dossier
        )
        assert rejected_ready is False
        assert any(
            "hypothesis_symbol_uninspected" in reason
            or "connected_mechanism_touchpoint_inspection_missing" in reason
            for reason in rejected_reasons
        ), rejected_reasons


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


@pytest.mark.parametrize(
    ("atom_value", "predicate"),
    [
        (5, {"kind": "range", "minimum": 3, "maximum": 7}),
        (False, {"kind": "equals", "expected": False}),
        (
            {"status": "broken", "attempts": 2},
            {
                "kind": "schema",
                "schema": {
                    "type": "object",
                    "required": ["status", "attempts"],
                    "properties": {
                        "status": {"type": "string"},
                        "attempts": {"type": "integer"},
                    },
                },
            },
        ),
        ({"exists": False}, {"kind": "existence", "expected": False}),
        (
            ["started", "failed"],
            {"kind": "event_sequence", "events": ["started", "failed"]},
        ),
    ],
)
def test_explicit_symptom_binding_accepts_registered_structured_predicates(
    atom_value: object,
    predicate: dict[str, object],
) -> None:
    snapshot = {"observed_symptom": atom_value}
    experiment = {
        "origin_evidence_bindings": [
            {
                "role": "symptom",
                "atom_id": "atom:structured",
                "field_path": "$.observed_symptom",
                "value": atom_value,
                "value_sha256": mod._canonical_json_sha256(atom_value),
                "observation_predicate": predicate,
            }
        ]
    }
    errors: list[str] = []

    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment=experiment,
        experiment_id="experiment:baseline",
        atom_id="atom:structured",
        atom_receipt={
            "atom_id": "atom:structured",
            "atom_sha256": mod._canonical_json_sha256(snapshot),
            "atom_snapshot": snapshot,
        },
        assertion={},
        command="runner verify",
        errors=errors,
    )

    assert errors == []
    assert direct is True
    assert len(bindings) == 1
    assert bindings[0]["observation_predicate"] == predicate
    assert bindings[0]["declared_binding_sha256"] == mod._canonical_json_sha256(
        {
            key: value
            for key, value in bindings[0].items()
            if key != "declared_binding_sha256"
        }
    )


def test_structured_atom_predicate_is_attested_against_adapter_baseline(
    tmp_path: Path,
) -> None:
    atom_id = "atom:attempt-count"
    snapshot = {"observed_attempts": 5, "expected_attempts": 1}
    atom_sha256 = mod._canonical_json_sha256(snapshot)
    assignment = {
        "atom_receipts": [
            {
                "atom_id": atom_id,
                "atom_sha256": atom_sha256,
                "atom_snapshot": snapshot,
            }
        ]
    }
    declaration = {
        "role": "symptom",
        "atom_id": atom_id,
        "field_path": "$.observed_attempts",
        "value": 5,
        "value_sha256": mod._canonical_json_sha256(5),
        "observation_predicate": {"kind": "equals", "expected": 5},
    }
    errors: list[str] = []
    bindings, direct = mod._explicit_atom_binding_receipts(
        experiment={"origin_evidence_bindings": [declaration]},
        experiment_id="experiment:baseline",
        atom_id=atom_id,
        atom_receipt=assignment["atom_receipts"][0],
        assertion={},
        command="runner verify",
        errors=errors,
    )
    assert errors == []
    assert direct is True

    def replay(experiment_id: str, attempts: int) -> dict[str, object]:
        stdout = tmp_path / f"{experiment_id.replace(':', '-')}.json"
        stderr = tmp_path / f"{experiment_id.replace(':', '-')}.stderr"
        stdout.write_text(json.dumps({"attempts": attempts}), encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        return {
            "experiment_id": experiment_id,
            "executed_argv": ["runner", "verify"],
            "exit_code": 0,
            "execution_isolation": {"platform": "windows"},
            "stdout_path": str(stdout),
            "stderr_path": str(stderr),
            "stdout_sha256": sha256(stdout.read_bytes()).hexdigest(),
            "stderr_sha256": sha256(stderr.read_bytes()).hexdigest(),
            "replay_inputs": mod._replay_inputs_receipt(
                source_experiment_id=experiment_id,
                environment_overrides={},
                disposable_state_paths=[],
            ),
        }

    claim = {
        "adapter_id": "structured_replay.v1",
        "hypothesis_id": "hypothesis:attempts",
        "baseline_experiment_id": "experiment:baseline",
        "challenge_experiment_id": "experiment:challenge",
        "intervention": {
            "kind": "attempt_policy",
            "target": "policy:attempts",
            "predicted_polarity": "excess_to_expected",
            "before": 5,
            "after": 1,
        },
        "observations": {
            "baseline": {"source": "stdout_json", "json_pointer": "/attempts"},
            "challenge": {"source": "stdout_json", "json_pointer": "/attempts"},
        },
        "positive_outcome": {
            "predicate": {"kind": "equals", "expected": 1},
            "semantic_basis": {
                "kind": "origin_exact_value",
                "atom_id": atom_id,
                "field_path": "$.expected_attempts",
            },
        },
    }
    experiments = {
        "experiment:baseline": {"experiment_id": "experiment:baseline"},
        "experiment:challenge": {
            "experiment_id": "experiment:challenge",
            "proof_adapter": claim,
        },
    }
    dossier = {
        "root_cause_hypotheses": [{"hypothesis_id": "hypothesis:attempts"}]
    }

    proofs, diagnostics = mod._proof_adapter_receipts(
        dossier,
        case_id="case:attempts",
        problem_id="problem:attempts",
        experiments=experiments,
        clean_replays={
            "experiment:baseline": replay("experiment:baseline", 5),
            "experiment:challenge": replay("experiment:challenge", 1),
        },
        evidence_assignment=assignment,
        atom_bindings=bindings,
        planning_workspace=None,
        symbol_receipts=[],
        artifact_receipts=[],
    )

    assert diagnostics == []
    assert len(proofs) == 1
    proof = proofs[0]
    attested = proof["source_root"]["atom_field_predicate_bindings"]
    assert len(attested) == 1
    assert attested[0]["atom_id"] == atom_id
    assert attested[0]["baseline_experiment_id"] == "experiment:baseline"
    assert attested[0]["runner_attested"] is True
    assert proof["replay_observation"]["selector"] == {
        "source": "stdout_json",
        "json_pointer": "/attempts",
    }
    assert proof["replay_inputs"]["source_experiment_id"] == "experiment:baseline"
    assert mod.validate_causal_proof_receipt(proof) == []


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


def _aggregate_mechanism_fixture(
    tmp_path: Path,
) -> tuple[
    dict[str, object],
    dict[str, dict[str, object]],
    list[dict[str, str]],
    list[dict[str, object]],
]:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def enter():\n    return bridge()\n\n"
        "def bridge():\n    return resolve()\n\n"
        "def resolve():\n    return 'reported symptom'\n",
        encoding="utf-8",
    )
    overlay = workspace / ".usertest_research"
    overlay.mkdir()
    (overlay / "entry.py").write_text(
        "from src.core import enter\nvalue = enter()\nprint(value)\n",
        encoding="utf-8",
    )
    (overlay / "bridge.py").write_text(
        "from src.core import enter\nprint(enter())\n",
        encoding="utf-8",
    )
    (overlay / "resolver.py").write_text(
        "from src.core import bridge\nprint(bridge())\n",
        encoding="utf-8",
    )

    def experiment(
        experiment_id: str,
        harness: str,
        *,
        mechanism_link: dict[str, object] | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "experiment_id": experiment_id,
            "scenario_kind": "faithful_replay",
            "command": f"python .usertest_research/{harness}",
            "outcome": "supports",
            "addresses_atom_ids": ["atom:one"],
            "artifact_refs": ["artifact:one"],
            "observable_assertion": {
                "source": "stdout",
                "operator": "equals",
                "expected": "reported symptom",
            },
        }
        if mechanism_link is not None:
            value["mechanism_link"] = mechanism_link
        return value

    enter_to_bridge = {
        "kind": "entrypoint_dataflow",
        "entrypoint": "core.enter",
        "code_path": [
            {
                "path": "src/core.py",
                "symbol": "core.enter",
                "observation": "The observed entrypoint calls the production bridge.",
            },
            {
                "path": "src/core.py",
                "symbol": "core.bridge",
                "observation": "The bridge continues the production failure path.",
            },
        ],
    }
    bridge_to_resolve = {
        "kind": "entrypoint_dataflow",
        "entrypoint": "core.bridge",
        "code_path": [
            {
                "path": "src/core.py",
                "symbol": "core.bridge",
                "observation": "The bridge calls the result-producing mechanism.",
            },
            {
                "path": "src/core.py",
                "symbol": "core.resolve",
                "observation": "The resolver produces the observed symptom.",
            },
        ],
    }

    experiments = [
        experiment("entry-support", "entry.py"),
        experiment(
            "bridge-support",
            "bridge.py",
            mechanism_link=enter_to_bridge,
        ),
        experiment(
            "resolver-support",
            "resolver.py",
            mechanism_link=bridge_to_resolve,
        ),
    ]
    dossier: dict[str, object] = {
        "research_status": "evidence_sufficient",
        "experiments": experiments,
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "mechanism_symbols": ["core.enter", "core.bridge", "core.resolve"],
                "supporting_evidence": [
                    "entry-support",
                    "bridge-support",
                    "resolver-support",
                ],
                "counterevidence": [],
            }
        ],
    }
    replays = {
        str(item["experiment_id"]): {
            "executed_argv": mod._parse_replay_argv(str(item["command"])),
            "workspace_dir": str(workspace),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": str(index + 1) * 64,
            "stderr_sha256": str(index + 3) * 64,
        }
        for index, item in enumerate(experiments)
    }
    symbol_receipts = [
        {"symbol": "core.enter", "path": "src/core.py"},
        {"symbol": "core.bridge", "path": "src/core.py"},
        {"symbol": "core.resolve", "path": "src/core.py"},
    ]
    atom_bindings: list[dict[str, object]] = [
        {
            "experiment_id": "entry-support",
            "atom_id": "atom:one",
            "match_kind": "faithful_atom_evidence_symptom",
            "origin_atom_sha256": "a" * 64,
        }
    ]
    return dossier, replays, symbol_receipts, atom_bindings


def test_primary_mechanism_coverage_aggregates_verified_multi_hop_supports(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert errors == []
    advancing = [item for item in receipts if item["adversarial_effect"] == "supports_selection"]
    assert {tuple(item["mechanism_symbols"]) for item in advancing} == {
        ("core.enter",),
        ("core.enter", "core.bridge"),
        ("core.bridge", "core.resolve"),
    }
    assert set().union(*(set(item["mechanism_symbols"]) for item in advancing)) == {
        "core.enter",
        "core.bridge",
        "core.resolve",
    }
    projection = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert projection[0] == {
        "schema_version": 3,
        "mechanism_symbols": ["core.bridge", "core.enter", "core.resolve"],
        "code_paths": [
            {"symbol": "core.bridge", "path": "src/core.py"},
            {"symbol": "core.enter", "path": "src/core.py"},
            {"symbol": "core.resolve", "path": "src/core.py"},
        ],
    }
    assert projection[2] is not None
    assert sorted(item["connection_kind"] for item in projection[2]["support_connectivity"]) == [
        "causal_root",
        "runner_verified_causal_edge",
        "runner_verified_causal_edge",
    ]
    assert len(projection[2]["causal_root_evidence_ids"]) == 1
    closures = mod._deterministic_mechanism_closure_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        mechanism_evidence=receipts,
    )
    assert len(closures) == 1
    assert closures[0]["support_experiment_ids"] == [
        "bridge-support",
        "entry-support",
        "resolver-support",
    ]
    assert closures[0]["mechanism_evidence_ids"] == sorted(
        receipt["mechanism_evidence_id"] for receipt in advancing
    )
    assert closures[0]["closure_basis"] == "rooted_connected_support_component"
    reversed_projection = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=list(reversed(receipts)),
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert reversed_projection == projection


def test_pair_print_harness_overlap_cannot_manufacture_production_connectivity(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    workspace = Path(str(replays["entry-support"]["workspace_dir"]))
    (workspace / "src" / "core.py").write_text(
        "def enter():\n    return 'reported symptom'\n\n"
        "def bridge():\n    return 'mechanism bridge'\n\n"
        "def resolve():\n    return 'root mechanism'\n",
        encoding="utf-8",
    )
    (workspace / ".usertest_research" / "bridge.py").write_text(
        "from src.core import bridge, enter\nprint(f'{enter()}|{bridge()}')\n",
        encoding="utf-8",
    )
    (workspace / ".usertest_research" / "resolver.py").write_text(
        "from src.core import bridge, resolve\nprint(f'{bridge()}|{resolve()}')\n",
        encoding="utf-8",
    )
    experiments = dossier["experiments"]
    assert isinstance(experiments, list)
    for experiment in experiments:
        assert isinstance(experiment, dict)
        experiment.pop("mechanism_link", None)
    experiments[1]["observable_assertion"]["expected"] = "reported symptom|mechanism bridge"
    experiments[2]["observable_assertion"]["expected"] = "mechanism bridge|root mechanism"
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    advancing = [
        receipt for receipt in receipts if receipt.get("adversarial_effect") == "supports_selection"
    ]
    assert [receipt["experiment_ids"] for receipt in advancing] == [["entry-support"]]
    assert {
        "primary_hypothesis_support_disconnected:h1:bridge-support",
        "primary_hypothesis_support_disconnected:h1:resolver-support",
        "primary_hypothesis_mechanism_coverage_incomplete:h1:core.bridge,core.resolve",
    }.issubset(set(errors))


def test_primary_mechanism_coverage_rejects_aggregate_symbol_omission(tmp_path: Path) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    dossier["experiments"] = dossier["experiments"][:2]
    replays.pop("resolver-support")
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert "primary_hypothesis_mechanism_coverage_incomplete:h1:core.resolve" in errors
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_primary_mechanism_coverage_requires_origin_symptom_entrypoint(tmp_path: Path) -> None:
    dossier, replays, symbol_receipts, _atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=[],
        errors=errors,
    )

    assert "primary_hypothesis_causal_root_missing:h1" in errors
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_disconnected_symbol_union_cannot_advance_or_create_positive_evidence(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    experiments = dossier["experiments"]
    assert isinstance(experiments, list)
    dossier["experiments"] = [experiments[0], experiments[2]]
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = ["core.enter", "core.resolve"]
    hypothesis["supporting_evidence"] = ["entry-support", "resolver-support"]
    workspace = Path(str(replays["entry-support"]["workspace_dir"]))
    (workspace / ".usertest_research" / "resolver.py").write_text(
        "from src.core import resolve\nprint(resolve())\n",
        encoding="utf-8",
    )
    assert isinstance(experiments[2], dict)
    experiments[2].pop("mechanism_link", None)
    replays.pop("bridge-support")
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert "primary_hypothesis_support_disconnected:h1:resolver-support" in errors
    assert "primary_hypothesis_mechanism_coverage_incomplete:h1:core.resolve" in errors
    advancing_experiments = {
        experiment_id
        for receipt in receipts
        if receipt.get("adversarial_effect") == "supports_selection"
        for experiment_id in receipt.get("experiment_ids", [])
    }
    assert advancing_experiments == {"entry-support"}
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_two_independently_rooted_disconnected_supports_cannot_union(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    experiments = dossier["experiments"]
    assert isinstance(experiments, list)
    dossier["experiments"] = [experiments[0], experiments[2]]
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = ["core.enter", "core.resolve"]
    hypothesis["supporting_evidence"] = ["entry-support", "resolver-support"]
    workspace = Path(str(replays["entry-support"]["workspace_dir"]))
    (workspace / ".usertest_research" / "resolver.py").write_text(
        "from src.core import resolve\nprint(resolve())\n",
        encoding="utf-8",
    )
    assert isinstance(experiments[2], dict)
    experiments[2].pop("mechanism_link", None)
    replays.pop("bridge-support")
    atom_bindings.append(
        {
            "experiment_id": "resolver-support",
            "atom_id": "atom:one",
            "match_kind": "faithful_atom_evidence_symptom",
            "origin_atom_sha256": "a" * 64,
        }
    )
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert len(receipts) == 1
    selected_experiment = receipts[0]["experiment_ids"][0]
    if selected_experiment == "entry-support":
        expected_errors = {
            "primary_hypothesis_support_disconnected:h1:resolver-support",
            "primary_hypothesis_mechanism_coverage_incomplete:h1:core.resolve",
        }
    else:
        assert selected_experiment == "resolver-support"
        expected_errors = {
            "primary_hypothesis_support_disconnected:h1:entry-support",
            "primary_hypothesis_mechanism_coverage_incomplete:h1:core.enter",
        }
    assert set(errors) == expected_errors
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_exact_immutable_source_command_can_root_connected_supports(tmp_path: Path) -> None:
    dossier, replays, symbol_receipts, _atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    entry_replay = replays["entry-support"]
    argv = entry_replay["executed_argv"]
    assert isinstance(argv, list)
    entry_replay["command_authorization"] = mod._command_authorization_receipt(
        {
            "authorization_kind": "immutable_source_command",
            "executed_argv_sha256": mod._canonical_json_sha256(argv),
            "shell": False,
            "workspace_confined": True,
            "origin_atom_id": "atom:one",
            "origin_atom_sha256": "a" * 64,
            "origin_atom_field_path": "$.command",
            "origin_command_value_sha256": "b" * 64,
        }
    )
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=[],
        errors=errors,
    )

    assert errors == []
    root = next(
        receipt for receipt in receipts if receipt.get("experiment_ids") == ["entry-support"]
    )
    assert root["causal_root_bindings"][0]["kind"] == "immutable_source_command"
    assert root["consumer_identity"] == {
        "kind": "research_harness",
        "entrypoint": ".usertest_research/entry.py",
    }
    projection = mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=receipts,
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    )
    assert projection[0] is not None
    assert projection[2] is not None
    assert len(projection[2]["causal_root_evidence_ids"]) == 1


def test_inspected_entrypoint_authorization_is_not_an_immutable_causal_root(
    tmp_path: Path,
) -> None:
    dossier, replays, symbol_receipts, _atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    entry_replay = replays["entry-support"]
    argv = entry_replay["executed_argv"]
    assert isinstance(argv, list)
    entry_replay["command_authorization"] = {
        "authorization_kind": "declared_inspected_repository_entrypoint",
        "executed_argv_sha256": mod._canonical_json_sha256(argv),
        "shell": False,
        "workspace_confined": True,
    }
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=[],
        errors=errors,
    )

    assert "primary_hypothesis_causal_root_missing:h1" in errors
    assert not any(
        receipt.get("adversarial_effect") == "supports_selection" for receipt in receipts
    )


@pytest.mark.parametrize(
    "mechanism_symbols",
    [
        ["core.enter", "core.enter"],
        ["core.enter", " core.enter "],
    ],
)
def test_duplicate_hypothesis_mechanism_symbols_are_rejected_before_evidence(
    tmp_path: Path,
    mechanism_symbols: list[str],
) -> None:
    dossier, replays, symbol_receipts, atom_bindings = _aggregate_mechanism_fixture(tmp_path)
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = mechanism_symbols
    errors: list[str] = []

    receipts = mod._typed_mechanism_evidence_receipts(
        dossier,
        clean_replays=replays,
        symbol_receipts=symbol_receipts,
        causal_links=[],
        strong_controls=[],
        falsification_interventions=[],
        deterministic_closures=[],
        atom_bindings=atom_bindings,
        errors=errors,
    )

    assert errors == ["hypothesis_mechanism_symbols_duplicate:h1:core.enter"]
    assert receipts == []
    assert mod._verified_mechanism_projection(
        dossier,
        mechanism_evidence=[],
        control_verifications=[],
        falsification_interventions=[],
        deterministic_closures=[],
    ) == (None, None, None, None)


def test_research_harness_identity_is_not_promoted_by_a_production_link() -> None:
    identity = mod._experiment_consumer_identity(
        experiment={"experiment_id": "support"},
        replay={"executed_argv": ["python", ".usertest_research/probe.py"]},
        mechanism_link={
            "verification_method": "runner_python_call_chain_v1",
            "entrypoint": "api.execute",
        },
        harness_path=".usertest_research/probe.py",
    )

    assert identity == {
        "kind": "research_harness",
        "entrypoint": ".usertest_research/probe.py",
    }


def _connectivity_edge_supports() -> list[dict[str, object]]:
    link: dict[str, object] = {
        "verification_method": "runner_python_call_chain_v1",
        "entrypoint": "core.enter",
        "code_path": [
            {"symbol": "core.enter", "path": "src/core.py", "observation": "caller"},
            {"symbol": "core.resolve", "path": "src/core.py", "observation": "callee"},
        ],
        "verified_call_edges": [
            {
                "caller_symbol": "core.enter",
                "caller_path": "src/core.py",
                "callee_symbol": "core.resolve",
                "callee_path": "src/core.py",
                "line": 4,
                "resolved_call": "core.resolve",
                "call_ast_sha256": "c" * 64,
            }
        ],
    }
    link["mechanism_link_sha256"] = mod._canonical_json_sha256(link)
    return [
        {
            "mechanism_evidence_id": "mechanism_evidence:root",
            "mechanism_symbols": ["core.enter"],
            "experiment_ids": ["root"],
            "causal_root_bindings": [
                {
                    "kind": "origin_symptom_observation",
                    "experiment_ids": ["root"],
                    "origin_atom_ids": ["atom:one"],
                    "root_mechanism_symbol": "core.enter",
                }
            ],
            "mechanism_link": None,
        },
        {
            "mechanism_evidence_id": "mechanism_evidence:tail",
            "mechanism_symbols": ["core.resolve"],
            "experiment_ids": ["tail"],
            "causal_root_bindings": [],
            "mechanism_link": link,
        },
    ]


def test_runner_minted_causal_edge_can_connect_disjoint_support_symbol_sets() -> None:
    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        _connectivity_edge_supports(),
        hypothesis_symbols=["core.enter", "core.resolve"],
    )

    assert len(connected) == 2
    assert symbols == {"core.enter", "core.resolve"}
    assert disconnected == []
    assert sorted(item["connection_kind"] for item in trace) == [
        "causal_root",
        "runner_verified_causal_edge",
    ]


def test_unattested_causal_edge_cannot_connect_disjoint_supports() -> None:
    supports = _connectivity_edge_supports()
    mechanism_link = supports[1]["mechanism_link"]
    assert isinstance(mechanism_link, dict)
    mechanism_link["mechanism_link_sha256"] = "0" * 64

    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.resolve"],
    )

    assert len(connected) == 1
    assert symbols == {"core.enter"}
    assert [item["connection_kind"] for item in trace] == ["causal_root"]
    assert disconnected == ["mechanism_evidence:tail"]


def test_runner_edge_cannot_leak_from_its_receipt_to_unrelated_support() -> None:
    supports = _connectivity_edge_supports()
    mechanism_link = supports[1]["mechanism_link"]
    supports[1]["mechanism_link"] = None
    supports.append(
        {
            "mechanism_evidence_id": "mechanism_evidence:edge-owner",
            "mechanism_symbols": ["core.other"],
            "experiment_ids": ["edge-owner"],
            "causal_root_bindings": [],
            "mechanism_link": mechanism_link,
        }
    )

    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.resolve", "core.other"],
    )

    assert [item["mechanism_evidence_id"] for item in connected] == ["mechanism_evidence:root"]
    assert symbols == {"core.enter"}
    assert [item["connection_kind"] for item in trace] == ["causal_root"]
    assert disconnected == ["mechanism_evidence:edge-owner", "mechanism_evidence:tail"]


def test_runner_minted_causal_edge_cannot_be_traversed_from_callee_to_caller() -> None:
    supports = _connectivity_edge_supports()
    supports[1]["causal_root_bindings"] = [
        {
            "kind": "origin_symptom_observation",
            "experiment_ids": ["tail"],
            "origin_atom_ids": ["atom:one"],
            "root_mechanism_symbol": "core.resolve",
        }
    ]
    supports[0]["causal_root_bindings"] = []

    connected, symbols, trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.resolve"],
    )

    assert [item["mechanism_evidence_id"] for item in connected] == ["mechanism_evidence:tail"]
    assert symbols == {"core.resolve"}
    assert [item["connection_kind"] for item in trace] == ["causal_root"]
    assert disconnected == ["mechanism_evidence:root"]


def test_root_selection_prefers_broader_component_then_lexical_tie() -> None:
    bridge_link: dict[str, object] = {
        "verification_method": "runner_python_call_chain_v1",
        "entrypoint": "core.bridge",
        "code_path": [
            {"symbol": "core.bridge", "path": "src/core.py", "observation": "caller"},
            {"symbol": "core.resolve", "path": "src/core.py", "observation": "callee"},
        ],
        "verified_call_edges": [
            {
                "caller_symbol": "core.bridge",
                "caller_path": "src/core.py",
                "callee_symbol": "core.resolve",
                "callee_path": "src/core.py",
                "line": 4,
                "resolved_call": "core.resolve",
                "call_ast_sha256": "d" * 64,
            }
        ],
    }
    bridge_link["mechanism_link_sha256"] = mod._canonical_json_sha256(bridge_link)
    supports = [
        {
            "mechanism_evidence_id": "mechanism_evidence:a",
            "mechanism_symbols": ["core.enter"],
            "experiment_ids": ["entry"],
            "causal_root_bindings": [
                {
                    "kind": "origin_symptom_observation",
                    "root_mechanism_symbol": "core.enter",
                }
            ],
        },
        {
            "mechanism_evidence_id": "mechanism_evidence:b",
            "mechanism_symbols": ["core.bridge"],
            "experiment_ids": ["bridge-root"],
            "causal_root_bindings": [
                {
                    "kind": "origin_symptom_observation",
                    "root_mechanism_symbol": "core.bridge",
                }
            ],
        },
        {
            "mechanism_evidence_id": "mechanism_evidence:c",
            "mechanism_symbols": ["core.bridge", "core.resolve"],
            "experiment_ids": ["bridge-tail"],
            "causal_root_bindings": [],
            "mechanism_link": bridge_link,
        },
    ]

    connected, symbols, _trace, disconnected = mod._rooted_support_connectivity(
        supports,
        hypothesis_symbols=["core.enter", "core.bridge", "core.resolve"],
    )

    assert [item["mechanism_evidence_id"] for item in connected] == [
        "mechanism_evidence:b",
        "mechanism_evidence:c",
    ]
    assert symbols == {"core.bridge", "core.resolve"}
    assert disconnected == ["mechanism_evidence:a"]

    tied, tied_symbols, _tied_trace, tied_disconnected = mod._rooted_support_connectivity(
        supports[:2],
        hypothesis_symbols=["core.enter", "core.bridge", "core.resolve"],
    )

    assert [item["mechanism_evidence_id"] for item in tied] == ["mechanism_evidence:a"]
    assert tied_symbols == {"core.enter"}
    assert tied_disconnected == ["mechanism_evidence:b"]


def test_control_can_verify_a_shared_nonempty_hypothesis_subset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    hypothesis = dossier["root_cause_hypotheses"][0]
    assert isinstance(hypothesis, dict)
    hypothesis["mechanism_symbols"] = ["core.run", "core.other"]
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[
            {"symbol": "core.run", "path": "src/core.py"},
            {"symbol": "core.other", "path": "src/core.py"},
        ],
        errors=errors,
    )

    assert errors == []
    assert len(controls) == 1
    assert controls[0]["mechanism_symbols"] == ["core.run"]
    assert controls[0]["support_verified_mechanism_symbols"] == ["core.run"]
    assert controls[0]["control_verified_mechanism_symbols"] == ["core.run"]


@pytest.mark.parametrize(
    ("relationship_symbols", "expected_error"),
    [
        (
            ["core.other"],
            "causal_control_mechanism_coverage_missing:h1:control",
        ),
        (
            ["outside.mode"],
            "causal_control_mechanism_subset_invalid:h1:control",
        ),
    ],
)
def test_control_rejects_mismatched_or_unverified_mechanism_subset(
    tmp_path: Path,
    relationship_symbols: list[str],
    expected_error: str,
) -> None:
    workspace = tmp_path / "workspace"
    _causal_control_repo(workspace)
    dossier, replays = _control_dossier("tests/test_core.py::test_guarded_control")
    hypothesis = dossier["root_cause_hypotheses"][0]
    control = dossier["experiments"][1]
    assert isinstance(hypothesis, dict)
    assert isinstance(control, dict)
    hypothesis["mechanism_symbols"] = ["core.run", "core.other"]
    relationship = control["control_relationship"]
    assert isinstance(relationship, dict)
    relationship["mechanism_symbols"] = relationship_symbols
    errors: list[str] = []

    _, controls = mod._causal_control_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[
            {"symbol": "core.run", "path": "src/core.py"},
            {"symbol": "core.other", "path": "src/core.py"},
        ],
        errors=errors,
    )

    assert controls == []
    assert expected_error in errors


def test_retained_harness_scalar_intervention_survives_with_runner_bound_flow(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def run(mode):\n    return 'bad'\n",
        encoding="utf-8",
    )
    (workspace / ".usertest_research").mkdir()
    harness = ".usertest_research/probe.py"
    (workspace / harness).write_text(
        "import sys\nfrom src.core import run\nvalue = run(sys.argv[1])\nprint(value)\n",
        encoding="utf-8",
    )

    def experiment(experiment_id: str, value: str, *, scenario_kind: str) -> dict[str, object]:
        result: dict[str, object] = {
            "experiment_id": experiment_id,
            "scenario_kind": scenario_kind,
            "addresses_atom_ids": ["atom:one"],
            "artifact_refs": ["artifact:one"],
            "command": f"python {harness} {value}",
            "outcome": "supports",
            "observable_assertion": {
                "source": "stdout",
                "operator": "equals",
                "expected": "bad",
            },
        }
        if scenario_kind == "control":
            result["control_relationship"] = {
                "supports_experiment_id": "baseline",
                "mechanism_symbols": ["core.run"],
                "controlled_variable": "mode scalar",
                "expected_difference": "Changing the mode should remove the failure.",
            }
        return result

    baseline = experiment("baseline", "legacy", scenario_kind="faithful_replay")
    challenge = experiment("challenge", "alternative", scenario_kind="control")
    dossier = {
        "experiments": [baseline, challenge],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "core.run ignores the mode and returns bad.",
                "mechanism_symbols": ["core.run"],
                "supporting_evidence": ["baseline", "challenge"],
                "counterevidence": [],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:scalar",
                        "hypothesis_id": "h1",
                        "claim": "core.run ignores the mode and returns bad.",
                        "baseline_experiment_id": "baseline",
                        "challenge_experiment_id": "challenge",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "equals",
                            "expected": "correct",
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
    }
    replays = {
        experiment_id: {
            "executed_argv": mod._parse_replay_argv(str(experiment["command"])),
            "workspace_dir": str(workspace),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": hash_character * 64,
            "stderr_sha256": "0" * 64,
        }
        for experiment_id, experiment, hash_character in (
            ("baseline", baseline, "1"),
            ("challenge", challenge, "2"),
        )
    }
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[{"symbol": "core.run", "path": "src/core.py"}],
        errors=errors,
    )

    assert errors == []
    assert len(receipts) == 1
    assert receipts[0]["verification_method"] == "runner_argv_falsification_intervention_v2"
    assert receipts[0]["mechanism_verification_mode"] == ("retained_harness_observable_dataflow")
    difference = receipts[0]["controlled_input_difference"]
    assert difference["verification_method"] == "retained_harness_scalar_argv_delta_v1"
    assert difference["difference"]["runtime_argv_index"] == 1
    assert difference["difference"]["mechanism_argument_bindings"][0]["symbol"] == "core.run"


@pytest.mark.parametrize(
    ("challenge_relative", "expected_mode", "expected_error"),
    [
        (
            ".usertest_research/challenge.py",
            None,
            "falsification_intervention_unverified:h1:attempt:subset",
        ),
        (
            "tools/challenge.py",
            None,
            "falsification_intervention_unverified:h1:attempt:subset",
        ),
    ],
)
def test_falsification_pair_requires_same_independently_verified_subset_mode(
    tmp_path: Path,
    challenge_relative: str,
    expected_mode: str | None,
    expected_error: str | None,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def run():\n    return 'bad'\n\ndef other():\n    return 'other'\n",
        encoding="utf-8",
    )
    (workspace / ".usertest_research").mkdir()
    baseline_relative = ".usertest_research/baseline.py"
    (workspace / baseline_relative).write_text(
        "from src.core import run\nvalue = run()\nprint(value)\n",
        encoding="utf-8",
    )
    challenge_path = workspace / challenge_relative
    challenge_path.parent.mkdir(parents=True, exist_ok=True)
    if challenge_relative.startswith(".usertest_research/"):
        challenge_path.write_text(
            "from src.core import run\nvalue = run()\nprint(value)\n",
            encoding="utf-8",
        )
        challenge_link = None
    else:
        challenge_path.write_text(
            "from src.core import run\n\ndef execute():\n    return run()\n\n"
            "if __name__ == '__main__':\n    print(execute())\n",
            encoding="utf-8",
        )
        challenge_link = {
            "kind": "entrypoint_dataflow",
            "entrypoint": "challenge.execute",
            "code_path": [
                {
                    "path": "tools/challenge.py",
                    "symbol": "challenge.execute",
                    "observation": "Calls the selected mechanism.",
                },
                {
                    "path": "src/core.py",
                    "symbol": "core.run",
                    "observation": "Returns the observed value.",
                },
            ],
        }
    baseline_command = f"python {baseline_relative}"
    challenge_command = f"python {challenge_relative}"
    baseline: dict[str, object] = {
        "experiment_id": "baseline",
        "scenario_kind": "faithful_replay",
        "addresses_atom_ids": ["atom:one"],
        "artifact_refs": ["artifact:one"],
        "command": baseline_command,
        "outcome": "supports",
        "observable_assertion": {
            "source": "stdout",
            "operator": "equals",
            "expected": "bad",
        },
    }
    challenge: dict[str, object] = {
        "experiment_id": "challenge",
        "scenario_kind": "control",
        "addresses_atom_ids": ["atom:one"],
        "artifact_refs": ["artifact:one"],
        "command": challenge_command,
        "outcome": "supports",
        "observable_assertion": {
            "source": "stdout",
            "operator": "equals",
            "expected": "bad",
        },
        "control_relationship": {
            "supports_experiment_id": "baseline",
            "mechanism_symbols": ["core.run"],
            "controlled_variable": "input program",
            "expected_difference": "The alternative program disproves the mechanism.",
        },
    }
    if challenge_link is not None:
        challenge["mechanism_link"] = challenge_link
    dossier: dict[str, object] = {
        "experiments": [baseline, challenge],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "core.run produces the wrong value.",
                "mechanism_symbols": ["core.run", "core.other"],
                "supporting_evidence": ["baseline", "challenge"],
                "counterevidence": [],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:subset",
                        "hypothesis_id": "h1",
                        "claim": "core.run produces the wrong value.",
                        "baseline_experiment_id": "baseline",
                        "challenge_experiment_id": "challenge",
                        "disproof_condition": {
                            "source": "stdout",
                            "operator": "equals",
                            "expected": "correct",
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
    }
    replays = {
        "baseline": {
            "executed_argv": mod._parse_replay_argv(baseline_command),
            "workspace_dir": str(workspace),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": "1" * 64,
            "stderr_sha256": "2" * 64,
        },
        "challenge": {
            "executed_argv": ["python", challenge_relative],
            "workspace_dir": str(workspace),
            "assertion_passed": True,
            "exit_code": 0,
            "stdout_sha256": "3" * 64,
            "stderr_sha256": "4" * 64,
        },
    }
    errors: list[str] = []

    receipts = mod._falsification_intervention_receipts(
        dossier,
        clean_replays=replays,
        planning_workspace=workspace,
        symbol_receipts=[
            {"symbol": "core.run", "path": "src/core.py"},
            {"symbol": "core.other", "path": "src/core.py"},
            {"symbol": "challenge.execute", "path": "tools/challenge.py"},
        ],
        errors=errors,
    )

    if expected_error is not None:
        assert receipts == []
        assert any(error.startswith(expected_error) for error in errors)
    else:
        assert errors == []
        assert len(receipts) == 1
        assert receipts[0]["mechanism_symbols"] == ["core.run"]
        assert receipts[0]["baseline_verified_mechanism_symbols"] == ["core.run"]
        assert receipts[0]["challenge_verified_mechanism_symbols"] == ["core.run"]
        assert receipts[0]["mechanism_verification_mode"] == expected_mode
