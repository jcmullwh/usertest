from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import runner_core.outcome_roles as outcome_roles
from runner_core.outcome_roles import (
    run_outcome_evidence_role,
    validate_outcome_evidence_role_artifact,
)


def _sha(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_repository_semantic_basis_allows_unrelated_file_edits_but_preserves_quote(
    tmp_path: Path,
) -> None:
    contract_path = tmp_path / "docs" / "contract.md"
    contract_path.parent.mkdir()
    subject = "materialized verification path"
    quote = f"The {subject} is readable by the implementing agent."
    contract_path.write_text(f"# Contract\n\n{quote}\n", encoding="utf-8")
    researched_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    oracle = {
        "positive_outcome_contracts": [
            {
                "positive_outcome_contract_id": "positive_outcome_contract:test",
                "kind": "retained_research_harness_assertion",
                "semantic_basis": {
                    "provenance": {
                        "kind": "repository_contract_quote",
                        "path": "docs/contract.md",
                        "sha256": researched_sha,
                        "exact_quote": quote,
                        "contract_locator": {
                            "kind": "mechanism_subject",
                            "subject": subject,
                        },
                    }
                },
            }
        ]
    }

    contract_path.write_text(
        f"# Contract\n\n{quote}\n\nUnrelated implementation note.\n",
        encoding="utf-8",
    )
    receipts = outcome_roles._verify_positive_contract_sources(oracle, workspace=tmp_path)
    assert receipts[0]["expected_sha256"] == researched_sha
    assert receipts[0]["observed_sha256"] != researched_sha
    assert receipts[0]["status"] == "verified"

    contract_path.write_text("# Contract\n\nThe cited behavior was removed.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outcome_role_semantic_basis_source_changed"):
        outcome_roles._verify_positive_contract_sources(oracle, workspace=tmp_path)


def test_repository_schema_basis_revalidates_pointer_value(tmp_path: Path) -> None:
    schema_path = tmp_path / "schemas" / "config.json"
    schema_path.parent.mkdir()
    quote = "Runtime mode required by the verified mechanism."
    schema = {
        "description": quote,
        "properties": {"mode": {"const": "safe"}},
    }
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    safe_hash = _sha("safe")
    oracle = {
        "positive_outcome_contracts": [
            {
                "positive_outcome_contract_id": "positive_outcome_contract:schema",
                "kind": "retained_research_harness_assertion",
                "semantic_basis": {
                    "provenance": {
                        "kind": "repository_contract_quote",
                        "path": "schemas/config.json",
                        "sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
                        "exact_quote": quote,
                        "contract_locator": {
                            "kind": "schema_pointer",
                            "json_pointer": "/properties/mode/const",
                            "value_sha256": safe_hash,
                        },
                    }
                },
            }
        ]
    }

    schema["unrelated"] = {"type": "string"}
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    receipts = outcome_roles._verify_positive_contract_sources(oracle, workspace=tmp_path)
    assert receipts[0]["status"] == "verified"

    schema["properties"]["mode"]["const"] = "unsafe"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(ValueError, match="outcome_role_semantic_basis_source_changed"):
        outcome_roles._verify_positive_contract_sources(oracle, workspace=tmp_path)


def test_oracle_asset_rejects_extra_directory_symlink(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    bundle = runs_root / "research" / "bundle"
    harness = bundle / ".usertest_research" / "repro.py"
    harness.parent.mkdir(parents=True)
    harness.write_text("print('ok')\n", encoding="utf-8")
    target = tmp_path / "outside-directory"
    target.mkdir()
    symlink = bundle / "extra-directory"
    try:
        os.symlink(target, symlink, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    manifest = {
        ".usertest_research/repro.py": {
            "kind": "file",
            "mode": harness.stat().st_mode & 0o777,
            "sha256": hashlib.sha256(harness.read_bytes()).hexdigest(),
            "size_bytes": harness.stat().st_size,
        }
    }
    asset = {
        "asset_id": "outcome_asset:"
        + _sha({"schema_version": 1, "manifest": manifest}),
        "runs_relative_path": "research/bundle",
        "manifest": manifest,
        "manifest_sha256": _sha(manifest),
    }

    with pytest.raises(ValueError, match="outcome_oracle_asset_entry_unsafe"):
        outcome_roles._verify_oracle_asset(asset, trusted_root=runs_root)


def test_role_executor_binds_commit_and_machine_predicates(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Tests")
    (workspace / "probe.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "Path('probe-result.json').write_text(json.dumps({'healthy': True}))\n"
        "print('scenario healthy')\n",
        encoding="utf-8",
    )
    _git(workspace, "add", "probe.py")
    _git(workspace, "commit", "-m", "probe")
    commit = _git(workspace, "rev-parse", "HEAD")
    unsigned_contract = {
        "description": "Replay the exact scenario and retain its machine-readable result.",
        "research_experiment_id": "experiment:original",
        "commands": ["python probe.py"],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "scenario healthy",
            },
            {
                "type": "command_stdout_not_contains",
                "command_index": 0,
                "value": "incorrect classification",
            },
            {
                "type": "artifact_json_value",
                "path": "probe-result.json",
                "json_pointer": "/healthy",
                "equals": True,
            },
        ],
    }
    contract = {
        **unsigned_contract,
        "role_contract_sha256": _sha(unsigned_contract),
    }
    output_path = tmp_path / "runs" / "original" / "outcome_role.json"

    artifact = run_outcome_evidence_role(
        workspace=workspace,
        output_path=output_path,
        role="original_scenario",
        role_contract=contract,
        case_id="case:one",
        plan_revision_id="plan:one:v1",
        merged_commit=commit,
        verification_contract_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        verified_implementation_head=commit,
        timeout_seconds=None,
    )

    assert artifact["passed"] is True
    assert artifact["timeout_seconds"] is None
    assert all(item["passed"] is True for item in artifact["predicate_results"])
    snapshot = Path(
        artifact["predicate_results"][3]["artifact_receipt"]["snapshot_path"]
    )
    assert snapshot.is_file()
    assert validate_outcome_evidence_role_artifact(
        json.loads(output_path.read_text(encoding="utf-8")),
        role="original_scenario",
        case_id="case:one",
        plan_revision_id="plan:one:v1",
        merged_commit=commit,
        verification_contract_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        verified_implementation_head=commit,
        role_contract=contract,
    )["passed"] is True


def test_role_executor_rejects_checkout_other_than_merged_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Tests")
    (workspace / "probe.py").write_text("print('ok')\n", encoding="utf-8")
    _git(workspace, "add", "probe.py")
    _git(workspace, "commit", "-m", "first")
    prior = _git(workspace, "rev-parse", "HEAD")
    (workspace / "next.txt").write_text("next\n", encoding="utf-8")
    _git(workspace, "add", "next.txt")
    _git(workspace, "commit", "-m", "next")
    unsigned_contract = {
        "description": "Exact check",
        "research_experiment_id": "experiment:original",
        "commands": ["python probe.py"],
        "predicates": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
    }
    contract = {**unsigned_contract, "role_contract_sha256": _sha(unsigned_contract)}

    try:
        run_outcome_evidence_role(
            workspace=workspace,
            output_path=tmp_path / "outcome_role.json",
            role="original_scenario",
            role_contract=contract,
            case_id="case:one",
            plan_revision_id="plan:one:v1",
            merged_commit=prior,
            verification_contract_sha256="a" * 64,
            target_contract_sha256="b" * 64,
            verified_implementation_head=prior,
        )
    except ValueError as exc:
        assert "workspace_commit_mismatch" in str(exc)
    else:
        raise AssertionError("Cross-commit outcome proof was accepted")


def _origin_positive_contract(*, experiment_id: str, expected: str) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": experiment_id,
        "mechanism_evidence_ids": [f"mechanism_evidence:{experiment_id}"],
        "origin_evidence": {
            "atom_id": f"atom:{experiment_id}",
            "atom_sha256": _sha({"expected_output": expected}),
            "field_path": "$.expected_output",
            "value_sha256": _sha(expected),
        },
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": expected,
            },
        ],
    }
    contract["positive_outcome_contract_id"] = (
        "positive_outcome_contract:" + _sha(contract)
    )
    return contract


def _staged_stdout_oracle(
    *,
    case_id: str,
    experiment_id: str,
    printed: str,
    expected: str,
) -> tuple[dict[str, object], dict[str, object]]:
    contract = _origin_positive_contract(
        experiment_id=experiment_id,
        expected=expected,
    )
    argv = ["python", "-c", f"print({printed!r})"]
    oracle: dict[str, object] = {
        "schema_version": 1,
        "case_id": case_id,
        "repo_revision": "research-revision",
        "research_experiment_id": experiment_id,
        "scenario_kind": "original_replay",
        "origin_atom_ids": [f"atom:{experiment_id}"],
        "mechanism_evidence_ids": [f"mechanism_evidence:{experiment_id}"],
        "baseline": {
            "exit_code": 1,
            "observable_assertion": {
                "source": "stderr",
                "operator": "contains",
                "expected": "original failure",
            },
            "stdout_sha256": "a" * 64,
            "stderr_sha256": "b" * 64,
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": argv,
            "command_authorization": {
                "authorization_kind": "declared_inspected_repository_entrypoint",
                "executed_argv_sha256": _sha(argv),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
        "positive_outcome_contracts": [contract],
    }
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _sha(oracle)
    return oracle, contract


def _multi_scenario_role_contract(
    pairs: list[tuple[dict[str, object], dict[str, object]]],
) -> dict[str, object]:
    scenarios: list[dict[str, object]] = []
    for oracle, contract in pairs:
        scenario: dict[str, object] = {
            "positive_outcome_contract_id": contract[
                "positive_outcome_contract_id"
            ],
            "oracle": oracle,
            "predicates": contract["postconditions"],
            "after_change": {},
        }
        scenario["scenario_id"] = "outcome_scenario:" + _sha(scenario)
        scenarios.append(scenario)
    outer: dict[str, object] = {
        "schema_version": 1,
        "kind": "multi_scenario",
        "case_id": "case:canonical",
        "repo_revision": "research-revision",
        "proof_scope": "multi_scenario",
        "positive_outcome_contracts": [contract for _, contract in pairs],
        "scenarios": scenarios,
    }
    outer["outcome_oracle_id"] = "outcome_oracle:" + _sha(outer)
    unsigned: dict[str, object] = {
        "description": "Replay every retained original scenario.",
        "research_experiment_id": "exp-one",
        "research_experiment_ids": [
            oracle["research_experiment_id"] for oracle, _ in pairs
        ],
        "selected_positive_outcome_contract_ids": [
            contract["positive_outcome_contract_id"] for _, contract in pairs
        ],
        "commands": [],
        "predicates": [
            {
                "type": "oracle_scenario_passed",
                "scenario_index": index,
                "scenario_id": scenario["scenario_id"],
            }
            for index, scenario in enumerate(scenarios)
        ],
        "oracle": outer,
        "required_proof_scope": "multi_scenario",
    }
    return {**unsigned, "role_contract_sha256": _sha(unsigned)}


def test_multi_scenario_oracle_requires_every_original_scenario(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Tests")
    (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "fixture")
    commit = _git(workspace, "rev-parse", "HEAD")
    first = _staged_stdout_oracle(
        case_id="case:one",
        experiment_id="exp-one",
        printed="scenario one healthy",
        expected="scenario one healthy",
    )
    second = _staged_stdout_oracle(
        case_id="case:two",
        experiment_id="exp-two",
        printed="scenario two healthy",
        expected="scenario two healthy",
    )
    role_contract = _multi_scenario_role_contract([first, second])
    output_path = tmp_path / "runs" / "multi" / "outcome_role.json"

    artifact = run_outcome_evidence_role(
        workspace=workspace,
        output_path=output_path,
        role="original_scenario",
        role_contract=role_contract,
        case_id="case:canonical",
        plan_revision_id="plan:canonical:v1",
        merged_commit=commit,
        verification_contract_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        verified_implementation_head=commit,
        timeout_seconds=None,
    )

    assert artifact["passed"] is True
    assert len(artifact["oracle_scenario_artifacts"]) == 2
    assert validate_outcome_evidence_role_artifact(
        json.loads(output_path.read_text(encoding="utf-8")),
        role="original_scenario",
        case_id="case:canonical",
        plan_revision_id="plan:canonical:v1",
        merged_commit=commit,
        verification_contract_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        verified_implementation_head=commit,
        role_contract=role_contract,
    )["passed"] is True

    shallow_second = _staged_stdout_oracle(
        case_id="case:two",
        experiment_id="exp-two",
        printed="failure swallowed",
        expected="scenario two healthy",
    )
    shallow_contract = _multi_scenario_role_contract([first, shallow_second])
    shallow = run_outcome_evidence_role(
        workspace=workspace,
        output_path=tmp_path / "runs" / "multi" / "shallow.json",
        role="original_scenario",
        role_contract=shallow_contract,
        case_id="case:canonical",
        plan_revision_id="plan:canonical:v1",
        merged_commit=commit,
        verification_contract_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        verified_implementation_head=commit,
        timeout_seconds=None,
    )
    assert shallow["passed"] is False
    assert shallow["oracle_scenario_artifacts"][0]["passed"] is True
    assert shallow["oracle_scenario_artifacts"][1]["passed"] is False


def _write_recurrence_refresh_receipt(
    root: Path,
    *,
    case_id: str,
    plan_revision_id: str,
    extra_evidence: bool = False,
    new_source_window: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    ids = ["1" * 64, "2" * 64]
    cycle_rows: list[dict[str, object]] = []
    state_cycles: list[dict[str, object]] = []
    for index, cycle_id in enumerate(ids, start=1):
        cycle_dir = root / f"cycle-{index}"
        cycle_dir.mkdir()
        evidence = ["atom:original", *( ["atom:recurrence"] if extra_evidence else [])]
        registry = {
            "schema_version": 1,
            "cases": {
                case_id: {
                    "case_id": case_id,
                    "case_revision": 2 if extra_evidence else 1,
                    "evidence_atom_ids": evidence,
                    "state": "original_scenario_verified",
                    "plan_revisions": {
                        plan_revision_id: {
                            "case_revision_at_plan": 1,
                            "evidence_atom_ids_at_plan": ["atom:original"],
                            "source_evidence_atom_ids_at_plan": ["atom:original"],
                        }
                    },
                    "current_lifecycle": {
                        "state": "original_scenario_verified",
                        "outcome_reference": {
                            "source": "provenance_verified_plan_outcome",
                            "plan_revision_id": plan_revision_id,
                        },
                    },
                }
            },
        }
        registry_path = cycle_dir / "case_registry.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        registry_hash = hashlib.sha256(registry_path.read_bytes()).hexdigest()
        generated_at = f"2026-07-10T12:0{index}:00Z"
        registry_receipt = {
            "name": "case_registry",
            "snapshot_path": str(registry_path),
            "sha256": registry_hash,
            "content_sha256": _sha(registry),
        }
        source_atoms = [
            {
                "atom_id": "atom:historical",
                "run_rel": "target/20260709/codex/0",
                "timestamp_utc": "2026-07-09T10:00:00Z",
                "evidence_role": "observation",
                "source": "command_failure",
            }
        ]
        if new_source_window:
            source_atoms.append(
                {
                    "atom_id": "atom:later-window",
                    "run_rel": "target/20260710/codex/0",
                    "timestamp_utc": "2026-07-10T12:00:30Z",
                    "evidence_role": "observation",
                    "source": "confusion_point",
                }
            )
        atoms_path = cycle_dir / "atoms.jsonl"
        atoms_path.write_text(
            "\n".join(json.dumps(atom) for atom in source_atoms) + "\n",
            encoding="utf-8",
        )
        atoms_hash = hashlib.sha256(atoms_path.read_bytes()).hexdigest()
        atoms_content_hash = _sha(source_atoms)
        source_runs = [
            {
                "run_rel": "target/20260709/codex/0",
                "source_atom_count": 1,
                "latest_timestamp_utc": "2026-07-09T10:00:00Z",
            },
            *(
                [
                    {
                        "run_rel": "target/20260710/codex/0",
                        "source_atom_count": 1,
                        "latest_timestamp_utc": "2026-07-10T12:00:30Z",
                    }
                ]
                if new_source_window
                else []
            ),
        ]
        source_window_unsigned = {
            "source_run_count": len(source_runs),
            "source_atom_count": len(source_atoms),
            "runs": source_runs,
        }
        source_window = {
            **source_window_unsigned,
            "summary_sha256": _sha(source_window_unsigned),
        }
        atoms_receipt = {
            "name": "atoms",
            "snapshot_path": str(atoms_path),
            "sha256": atoms_hash,
            "content_sha256": atoms_content_hash,
        }
        cycle_receipt_doc = {
            "cycle_id": cycle_id,
            "generated_at": generated_at,
            "passed": True,
            "artifact_receipts": [registry_receipt, atoms_receipt],
        }
        cycle_receipt_path = cycle_dir / "cycle_receipt.json"
        cycle_receipt_path.write_text(json.dumps(cycle_receipt_doc), encoding="utf-8")
        cycle_receipt_hash = hashlib.sha256(cycle_receipt_path.read_bytes()).hexdigest()
        cycle_rows.append(
            {
                "cycle_id": cycle_id,
                "generated_at": generated_at,
                "passed": True,
                "cycle_receipt_path": str(cycle_receipt_path),
                "cycle_receipt_sha256": cycle_receipt_hash,
                "case_registry_snapshot_path": str(registry_path),
                "case_registry_sha256": registry_hash,
                "case_registry_content_sha256": _sha(registry),
                "atoms_snapshot_path": str(atoms_path),
                "atoms_sha256": atoms_hash,
                "atoms_content_sha256": atoms_content_hash,
                "source_observation_window": source_window,
            }
        )
        state_cycles.append({"cycle_id": cycle_id})
    state = {
        "ready_for_export": True,
        "consecutive_stable_passes": 2,
        "cycles": state_cycles,
    }
    state_path = root / "shadow_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    receipt = {
        "schema_version": 3,
        "producer": "usertest_implement.backlog_refresh",
        "recorded_at_utc": "2026-07-10T12:03:00Z",
        "qualifying_cycle_ids": ids,
        "qualifying_cycles": cycle_rows,
        "shadow_state_path": str(state_path),
        "shadow_state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
    }
    receipt["receipt_content_sha256"] = _sha(receipt)
    receipt_path = root / "refresh_receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt_path


def test_recurrence_requires_later_shadow_case_proof_not_just_command(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "tests@example.com")
    _git(workspace, "config", "user.name", "Tests")
    (workspace / "probe.py").write_text("print('no recurrence')\n", encoding="utf-8")
    _git(workspace, "add", "probe.py")
    _git(workspace, "commit", "-m", "probe")
    commit = _git(workspace, "rev-parse", "HEAD")
    unsigned = {
        "description": "Observe recurrence through the canonical backlog case.",
        "commands": [],
        "predicates": [],
    }
    contract = {**unsigned, "role_contract_sha256": _sha(unsigned)}
    common = {
        "workspace": workspace,
        "output_path": tmp_path / "runs" / "recurrence" / "outcome_role.json",
        "role": "recurrence",
        "role_contract": contract,
        "case_id": "case:one",
        "plan_revision_id": "plan:one:v1",
        "merged_commit": commit,
        "verification_contract_sha256": "a" * 64,
        "target_contract_sha256": "b" * 64,
        "verified_implementation_head": commit,
    }
    with pytest.raises(ValueError, match="fresh_shadow_receipt_required"):
        run_outcome_evidence_role(**common)

    receipt = _write_recurrence_refresh_receipt(
        tmp_path / "refresh",
        case_id="case:one",
        plan_revision_id="plan:one:v1",
    )
    artifact = run_outcome_evidence_role(
        **common,
        recurrence_refresh_receipt_path=receipt,
        recurrence_after="2026-07-10T12:00:00Z",
    )
    assert artifact["passed"] is True
    assert artifact["commands"] == []
    assert len(artifact["recurrence_refresh_proof"]["qualifying_cycles"]) == 2
    assert artifact["recurrence_refresh_proof"]["new_source_observation_runs"] == [
        {
            "run_rel": "target/20260710/codex/0",
            "source_atom_count": 1,
            "latest_timestamp_utc": "2026-07-10T12:00:30Z",
        }
    ]
    assert validate_outcome_evidence_role_artifact(
        json.loads(common["output_path"].read_text(encoding="utf-8")),
        role="recurrence",
        case_id="case:one",
        plan_revision_id="plan:one:v1",
        merged_commit=commit,
        verification_contract_sha256="a" * 64,
        target_contract_sha256="b" * 64,
        verified_implementation_head=commit,
        role_contract=contract,
    )["passed"] is True

    recurrent_receipt = _write_recurrence_refresh_receipt(
        tmp_path / "recurrent-refresh",
        case_id="case:one",
        plan_revision_id="plan:one:v1",
        extra_evidence=True,
    )
    with pytest.raises(ValueError, match="new_case_evidence_detected"):
        run_outcome_evidence_role(
            **{**common, "output_path": tmp_path / "recurrent.json"},
            recurrence_refresh_receipt_path=recurrent_receipt,
            recurrence_after="2026-07-10T12:00:00Z",
        )

    no_window_receipt = _write_recurrence_refresh_receipt(
        tmp_path / "no-window-refresh",
        case_id="case:one",
        plan_revision_id="plan:one:v1",
        new_source_window=False,
    )
    with pytest.raises(ValueError, match="no_new_source_observation_window"):
        run_outcome_evidence_role(
            **{**common, "output_path": tmp_path / "no-window.json"},
            recurrence_refresh_receipt_path=no_window_receipt,
            recurrence_after="2026-07-10T12:00:00Z",
        )
