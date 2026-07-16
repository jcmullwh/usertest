from __future__ import annotations

import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path, PurePosixPath

import pytest
import yaml
from backlog_core import (
    assign_plan_revision_id,
    bind_falsification_review,
    bind_plan_outcome_oracle,
    evidence_assignment_sha256,
    evidence_verification_sha256,
    research_claims_sha256,
)
from backlog_repo import parse_verification_contract_markdown, upsert_outcome_markdown
from backlog_repo.export import ticket_export_case_id, ticket_export_fingerprint
from backlog_repo.plan_scope import parse_plan_target_contract_markdown

import usertest_backlog.commands.export_tickets as export_commands
from usertest_backlog.cli import _cleanup_stale_ticket_idea_files, main
from usertest_backlog.commands.plan_cleanup import (
    _cleanup_stale_generated_scope_ticket_files,
    _extract_generated_ticket_scope_metadata,
    _refresh_generated_ticket_idea_file,
)
from usertest_backlog.workflows.qualification import evaluate_independent_qualification
from usertest_backlog.workflows.shadow_validation import (
    record_shadow_cycle,
    shadow_state_path,
)

_REAL_EXPORT_SCOPE_ERRORS = export_commands._export_scope_errors


def test_awaiting_outcome_is_case_local_and_failed_solution_can_reenter_research() -> None:
    index = {
        "fp-waiting": {
            "active_outcome_records": [
                {
                    "case_id": "case:waiting-live",
                    "state": "unverified",
                    "remaining_risks": [
                        "Post-merge outcome verification is blocked: live service unavailable"
                    ],
                }
            ]
        },
        "fp-progressing": {
            "active_outcome_records": [{"case_id": "case:progressing", "state": "tests_verified"}]
        },
        "fp-failed": {
            "active_outcome_records": [
                {
                    "case_id": "case:solution-failed",
                    "state": "unverified",
                    "remaining_risks": [
                        "Post-merge outcome verification is blocked: original_scenario "
                        "did not satisfy its causal predicates"
                    ],
                }
            ]
        },
        "fp-mitigated": {
            "active_outcome_records": [{"case_id": "case:mitigated", "state": "mitigated"}]
        },
    }

    assert export_commands._case_ids_awaiting_outcome_verification(index) == {
        "case:progressing",
        "case:waiting-live",
    }


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _canonical_sha256(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _attach_exact_origin_boundary(
    *,
    research: dict[str, object],
    verification: dict[str, object],
    oracle: dict[str, object],
    positive_contract: dict[str, object],
    mechanism_evidence_ids: list[str],
    atom_id: str,
) -> None:
    experiment_id = str(oracle["research_experiment_id"])
    replay = next(
        value
        for value in verification["experiments"]
        if value["experiment_id"] == experiment_id
    )
    atom_receipt = next(
        value
        for value in research["evidence_assignment"]["atom_receipts"]
        if value["atom_id"] == atom_id
    )
    atom_snapshot = atom_receipt["atom_snapshot"]
    command = str(atom_snapshot["command"])
    argv = list(replay["executed_argv"])
    authorization = {
        "authorization_kind": "fixture_exact_origin_command",
        "executed_argv_sha256": _canonical_sha256(argv),
        "shell": False,
        "workspace_confined": True,
        "origin_atom_id": atom_id,
        "origin_atom_sha256": atom_receipt["atom_sha256"],
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": _canonical_sha256(command),
        "runner_attested": True,
    }
    authorization["authorization_sha256"] = _canonical_sha256(authorization)
    replay_inputs = {
        "schema_version": 1,
        "source_experiment_id": experiment_id,
        "environment": {},
        "disposable_state_paths": [],
        "runner_approved": True,
    }
    replay_inputs["replay_inputs_sha256"] = _canonical_sha256(replay_inputs)
    contract_id = str(positive_contract["positive_outcome_contract_id"])
    replay_observation = {
        "schema_version": 1,
        "source_experiment_id": experiment_id,
        "selector": {"source": "exit_code"},
        "source_observation_sha256": _canonical_sha256(
            {
                "exit_code": replay["exit_code"],
                "stdout_sha256": replay["stdout_sha256"],
                "stderr_sha256": replay["stderr_sha256"],
            }
        ),
        "predicate_input_mode": "post_change_observation",
        "positive_outcome_contract_ids": [contract_id],
        "runner_attested": True,
    }
    replay_observation["replay_observation_sha256"] = _canonical_sha256(
        replay_observation
    )
    replay["command_authorization"] = authorization
    replay["replay_inputs"] = replay_inputs
    oracle["execution"] = {
        "argv": argv,
        "command_authorization": authorization,
        "platform_requirement": "any",
        "shell": False,
        "replay_inputs": replay_inputs,
        "replay_observation": replay_observation,
    }
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _canonical_sha256(oracle)
    source_identity = {
        "schema_version": 1,
        "origin_atom_id": atom_id,
        "origin_atom_sha256": atom_receipt["atom_sha256"],
        "origin_atom_field_path": "$.command",
        "origin_command_value_sha256": _canonical_sha256(command),
        "executed_argv_sha256": authorization["executed_argv_sha256"],
        "command_authorization_sha256": authorization["authorization_sha256"],
        "runner_attested": True,
    }
    source_identity["source_identity_sha256"] = _canonical_sha256(source_identity)
    equivalence = {
        "schema_version": 1,
        "equivalence_mode": "exact_origin_scenario_identity",
        "source_experiment_id": experiment_id,
        "origin_atom_ids": [atom_id],
        "source_identity": source_identity,
        "source_identity_refs": [
            f"origin_command_identity:{source_identity['source_identity_sha256']}"
        ],
        "replay_inputs_sha256": replay_inputs["replay_inputs_sha256"],
        "replay_observation_sha256": replay_observation[
            "replay_observation_sha256"
        ],
        "positive_outcome_contract_ids": [contract_id],
        "selected_mechanism_evidence_ids": sorted(mechanism_evidence_ids),
        "outcome_oracle_id": oracle["outcome_oracle_id"],
        "runner_attested": True,
    }
    equivalence["equivalence_sha256"] = _canonical_sha256(equivalence)
    replay_projection = {
        "experiment_id": experiment_id,
        "executed_argv_sha256": authorization["executed_argv_sha256"],
        "command_authorization_sha256": authorization["authorization_sha256"],
        "stdout_sha256": replay["stdout_sha256"],
        "stderr_sha256": replay["stderr_sha256"],
        "replay_inputs_sha256": replay_inputs["replay_inputs_sha256"],
        "execution_isolation_sha256": _canonical_sha256(replay["execution_isolation"]),
    }
    boundary = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "boundary_kind": "fixture/repository-original-scenario",
        "requires_live_verification": False,
        "faithful_equivalence": True,
        "provenance_refs": sorted(
            {
                f"research_experiment:{experiment_id}",
                f"clean_replay:{_canonical_sha256(replay_projection)}",
                *mechanism_evidence_ids,
                str(oracle["outcome_oracle_id"]),
                f"equivalence_proof:{equivalence['equivalence_sha256']}",
            }
        ),
        "rationale_sha256": _canonical_sha256("exact original fixture identity"),
        "runner_attested": True,
        "equivalence_proof": equivalence,
    }
    boundary["boundary_sha256"] = _canonical_sha256(boundary)
    verification["verification_boundaries"] = [boundary]


def _write_yaml(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(obj, sort_keys=False), encoding="utf-8")


def _shadow_cycle_artifacts(tmp_path: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name in (
        "atoms",
        "problem_records",
        "problem_mining_evidence",
        "prioritized_problems",
        "research",
        "solution_options",
        "solution_selection",
        "change_plans",
        "case_registry",
    ):
        path = tmp_path / "shadow-cycle-inputs" / f"{name}.json"
        if not path.exists():
            _write_json(path, {"artifact": name})
        paths[name] = path
    return paths


def _make_repo_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True, exist_ok=True)
    _write_yaml(repo_root / "configs" / "agents.yaml", {"agents": {}})
    _write_yaml(repo_root / "configs" / "policies.yaml", {"policies": {}})
    _write_yaml(
        repo_root / "configs" / "backlog_export_gate.yaml",
        {
            "backlog_export_gate": {
                "enabled": True,
                "required_consecutive_shadow_cycles": 1,
                "require_exact_export_projection": True,
            }
        },
    )
    _write_yaml(
        repo_root / "configs" / "backlog_policy.yaml",
        {
            "backlog_policy": {
                "surface_area_high": [
                    "new_command",
                    "breaking_change",
                    "new_top_level_mode",
                    "new_config_schema",
                    "new_api",
                ],
                "breadth_min_for_surface_area_high": {
                    "missions": 2,
                    "targets": 2,
                    "repo_inputs": 2,
                },
                "default_stage_for_high_surface_low_breadth": "research_required",
                "default_stage_for_labeled": "ready_for_ticket",
                "investigation_steps_for_high_surface_low_breadth": [
                    "Validate repo intent",
                    "Check if existing commands/flags can be parameterized",
                    "Propose a consolidation plan (avoid new top-level commands)",
                ],
            }
        },
    )
    _write_yaml(
        repo_root / "configs" / "backlog_policy_internal_maintenance.yaml",
        {
            "backlog_policy": {
                "surface_area_high": [
                    "new_command",
                    "breaking_change",
                    "new_top_level_mode",
                    "new_config_schema",
                    "new_api",
                ],
                "high_surface_rules": [
                    {
                        "rule_id": "command_surface",
                        "applies_to_kinds": [
                            "new_command",
                            "new_top_level_mode",
                            "new_config_schema",
                        ],
                        "breadth_min": {
                            "missions": 2,
                            "targets": 2,
                            "repo_inputs": 2,
                        },
                        "default_stage_for_low_breadth": "research_required",
                        "investigation_steps": [
                            "Validate repo intent",
                            "Check if existing commands/flags can be parameterized",
                        ],
                        "risk_tag": "surface_change",
                        "review_domain": "command_surface",
                    },
                    {
                        "rule_id": "behavior_compat",
                        "applies_to_kinds": ["breaking_change", "new_api"],
                        "breadth_min": {"runs": 5, "agents": 2},
                        "default_stage_for_low_breadth": "research_required",
                        "investigation_steps": [
                            "Validate compatibility implications",
                        ],
                        "risk_tag": "behavior_change",
                        "review_domain": "behavior_compat",
                    },
                ],
                "default_stage_for_labeled": "ready_for_ticket",
            }
        },
    )
    return repo_root


def _bind_shadow_export_contract(
    *,
    repo_root: Path,
    backlog_path: Path,
    tmp_path: Path,
    generated_at: str,
    required_consecutive_cycles: int = 1,
) -> None:
    backlog = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert isinstance(backlog, dict)
    stage_paths = _shadow_cycle_artifacts(tmp_path)
    artifacts_raw = backlog.get("artifacts")
    artifacts = dict(artifacts_raw) if isinstance(artifacts_raw, dict) else {}
    artifacts.update(
        {
            "atoms_jsonl": str(stage_paths["atoms"]),
            "case_registry_json": str(stage_paths["case_registry"]),
            "six_stage_pipeline": {
                "problem_records_json": str(stage_paths["problem_records"]),
                "problem_mining_evidence_json": str(stage_paths["problem_mining_evidence"]),
                "prioritized_problems_json": str(stage_paths["prioritized_problems"]),
                "research_json": str(stage_paths["research"]),
                "solution_options_json": str(stage_paths["solution_options"]),
                "solution_selection_json": str(stage_paths["solution_selection"]),
                "change_plans_json": str(stage_paths["change_plans"]),
                "case_registry_json": str(stage_paths["case_registry"]),
            },
        }
    )
    backlog["artifacts"] = artifacts
    policy_path = repo_root / "configs" / "backlog_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))["backlog_policy"]
    projection = export_commands._build_export_projection(
        backlog=backlog,
        surface_area_high=set(policy["surface_area_high"]),
        cli_repo_input=None,
        repo_root=repo_root,
    )
    artifacts["export_contract"] = {
        "schema_version": 1,
        "projection_sha256": projection["sha256"],
        "policy_config_path": str(policy_path.resolve()),
        "ux_review_json_path": str(
            export_commands._ux_review_path_for_backlog(backlog_path).resolve()
        ),
    }
    _write_json(backlog_path, backlog)
    artifact_paths = export_commands._export_artifact_paths(
        backlog=backlog,
        backlog_path=backlog_path,
        repo_root=repo_root,
        policy_config_path=policy_path,
        export_gate_config_path=repo_root / "configs" / "backlog_export_gate.yaml",
        cli_repo_input=None,
    )
    qualification = evaluate_independent_qualification(
        atoms=[],
        accepted_outputs_by_kind={},
        positive_throughput_required=False,
    )
    qualification["policy"]["positive_throughput_required"] = True
    qualification["counts"]["actionable_cases"] = 1
    qualification["counts"]["positive_qualifying_corpus"] = 1
    qualification["counts"]["exhausted_corpus"] = 0
    qualification["status"] = "verified"
    qualification["qualification_class"] = "positive_throughput"
    qualification["failures"] = []
    qualification["correction_routing_status"] = "not_required"
    invariant_report = {
        "schema_version": 4,
        "cycle_mode": "release",
        "passed": True,
        "failures": [],
        "checks": {},
        "atom_corpus_sha256": "a" * 64,
        "source_atom_corpus_sha256": "a" * 64,
        "case_graph_sha256": "b" * 64,
        "ticket_set_sha256": "c" * 64,
        "research_proof_basis_sha256": "d" * 64,
        "qualification_basis_sha256": qualification["basis_sha256"],
        "qualification_stability_sha256": qualification["stability_sha256"],
        "export_projection_sha256": projection["sha256"],
        "qualification": qualification,
        "counts": {},
    }
    record_shadow_cycle(
        state_path=shadow_state_path(backlog_path),
        backlog_path=backlog_path,
        invariant_report=invariant_report,
        artifact_paths=artifact_paths,
        generated_at=generated_at,
        required_consecutive_cycles=required_consecutive_cycles,
    )


_SYNTHETIC_RESEARCH_IDS: set[tuple[str, str]] = set()


@pytest.fixture(autouse=True)
def _verify_only_real_or_explicit_synthetic_research(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Keep export-behavior fixtures focused while production revalidates real files."""
    _SYNTHETIC_RESEARCH_IDS.clear()
    real_verifier = export_commands.verify_persisted_research_evidence
    real_shadow_validator = export_commands.validate_shadow_export_state
    real_projection_validator = export_commands._shadow_projection_binding_errors
    real_scope_validator = export_commands._export_scope_errors

    def verify(dossier: dict[str, object]) -> tuple[bool, list[str]]:
        identity = (str(dossier.get("case_id") or ""), str(dossier.get("problem_id") or ""))
        if identity in _SYNTHETIC_RESEARCH_IDS:
            return True, []
        return real_verifier(dossier)

    monkeypatch.setattr(
        export_commands,
        "verify_persisted_research_evidence",
        verify,
    )

    def validate_shadow(**kwargs: object) -> tuple[bool, list[str], dict[str, object] | None]:
        state_path = kwargs.get("state_path")
        required = kwargs.get("required_consecutive_cycles")
        if isinstance(state_path, Path) and (state_path.exists() or required != 1):
            return real_shadow_validator(**kwargs)  # type: ignore[arg-type]
        return True, [], {"_fixture_unbound_shadow": True, "consecutive_stable_passes": 1}

    def validate_projection(**kwargs: object) -> list[str]:
        gate_state = kwargs.get("gate_state")
        if isinstance(gate_state, dict) and gate_state.get("_fixture_unbound_shadow") is True:
            return []
        return real_projection_validator(**kwargs)  # type: ignore[arg-type]

    def validate_scope(**kwargs: object) -> list[str]:
        scope = kwargs.get("backlog_scope")
        if not isinstance(scope, dict) or "target" not in scope:
            return []
        return real_scope_validator(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(export_commands, "validate_shadow_export_state", validate_shadow)
    monkeypatch.setattr(
        export_commands,
        "_shadow_projection_binding_errors",
        validate_projection,
    )
    monkeypatch.setattr(export_commands, "_export_scope_errors", validate_scope)
    yield
    _SYNTHETIC_RESEARCH_IDS.clear()


def test_export_gate_honors_configured_stable_shadow_cycle_count(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    _write_yaml(
        repo_root / "configs" / "backlog_export_gate.yaml",
        {
            "backlog_export_gate": {
                "enabled": True,
                "required_consecutive_shadow_cycles": 3,
                "require_exact_export_projection": True,
            }
        },
    )
    runs_dir = tmp_path / "runs"
    backlog_path = runs_dir / "target_a" / "_compiled" / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target_a", "repo_input": None},
            "tickets": [],
        },
    )
    export_args = [
        "reports",
        "export-tickets",
        "--repo-root",
        str(repo_root),
        "--runs-dir",
        str(runs_dir),
        "--target",
        "target_a",
        "--backlog-json",
        str(backlog_path),
        "--actions-yaml",
        str(tmp_path / "actions.yaml"),
        "--atom-actions-yaml",
        str(tmp_path / "atom_actions.yaml"),
    ]

    with pytest.raises(SystemExit) as exc:
        main(export_args)
    assert exc.value.code == 2
    assert not (runs_dir / "target_a" / "_compiled" / "target_a.tickets_export.json").exists()

    for generated_at in ("2026-07-09T00:00:00Z", "2026-07-09T01:00:00Z"):
        _bind_shadow_export_contract(
            repo_root=repo_root,
            backlog_path=backlog_path,
            tmp_path=tmp_path,
            generated_at=generated_at,
            required_consecutive_cycles=3,
        )

    with pytest.raises(SystemExit) as exc:
        main(export_args)
    assert exc.value.code == 2

    _bind_shadow_export_contract(
        repo_root=repo_root,
        backlog_path=backlog_path,
        tmp_path=tmp_path,
        generated_at="2026-07-09T02:00:00Z",
        required_consecutive_cycles=3,
    )

    with pytest.raises(SystemExit) as exc:
        main(export_args)
    assert exc.value.code == 0


def test_sealed_qualification_rejects_live_atom_ledger_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_ledger = tmp_path / "live_atom_actions.yaml"
    live_ledger.write_text("atom:one:\n  status: new\n", encoding="utf-8")
    original = live_ledger.read_bytes()
    expected_sha256 = sha256(original).hexdigest()
    bundle_path = tmp_path / "qualification_input_bundle.json"
    bundle_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        export_commands,
        "load_qualification_input_bundle",
        lambda _path, *, verify_files: {
            "source_inputs": {
                "atom_actions": {
                    "sha256": expected_sha256,
                    "size_bytes": len(original),
                }
            }
        },
    )

    assert export_commands._sealed_live_atom_actions_snapshot(
        qualification_input_bundle_path=bundle_path,
        live_atom_actions_path=live_ledger,
    ) == (original, expected_sha256)

    live_ledger.write_text("atom:one:\n  status: actioned\n", encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="qualification_live_atom_actions_changed_since_prepare",
    ):
        export_commands._sealed_live_atom_actions_snapshot(
            qualification_input_bundle_path=bundle_path,
            live_atom_actions_path=live_ledger,
        )


@pytest.mark.parametrize("gate_mode", ["missing", "disabled"])
def test_export_gate_missing_or_disabled_fails_closed_before_mutation(
    tmp_path: Path,
    gate_mode: str,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    gate_path = repo_root / "configs" / "backlog_export_gate.yaml"
    if gate_mode == "missing":
        gate_path.unlink()
    else:
        _write_yaml(
            gate_path,
            {
                "backlog_export_gate": {
                    "enabled": False,
                    "required_consecutive_shadow_cycles": 1,
                    "require_exact_export_projection": True,
                }
            },
        )
    compiled = tmp_path / "runs" / "target_a" / "_compiled"
    backlog_path = compiled / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target_a", "repo_input": None},
            "tickets": [],
        },
    )
    atom_actions_path = tmp_path / "atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--backlog-json",
                str(backlog_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )

    assert exc.value.code == 2
    assert not atom_actions_path.exists()
    assert not (compiled / "target_a.tickets_export.json").exists()


@pytest.mark.parametrize(
    "scope",
    [None, {"target": "other_target", "repo_input": None}],
)
def test_export_rejects_missing_or_mismatched_canonical_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: dict[str, object] | None,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    backlog_path = tmp_path / "runs" / "target_a" / "_compiled" / "target_a.backlog.json"
    backlog: dict[str, object] = {"schema_version": 1, "tickets": []}
    if scope is not None:
        backlog["scope"] = scope
    _write_json(backlog_path, backlog)
    monkeypatch.setattr(
        export_commands,
        "_export_scope_errors",
        _REAL_EXPORT_SCOPE_ERRORS,
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
            ]
        )

    assert exc.value.code == 2


def test_export_rejects_legacy_one_pass_analysis_before_shadow_gate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = _make_repo_root(tmp_path)
    backlog_path = tmp_path / "runs" / "target_a" / "_compiled" / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "pipeline_kind": "legacy_one_pass_analysis",
            "analysis_only": True,
            "export_eligible": False,
            "tickets": [],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
            ]
        )

    assert exc.value.code == 2
    assert "analysis-only" in capsys.readouterr().err


def test_export_accepts_explicit_global_scope(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    backlog_path = tmp_path / "global.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": None, "repo_input": None},
            "tickets": [],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--backlog-json",
                str(backlog_path),
            ]
        )

    assert exc.value.code == 0
    assert (tmp_path / "runs" / "_compiled" / "all.tickets_export.json").exists()


@pytest.mark.parametrize(
    "unsafe_flag",
    ["--include-actioned", "--include-discarded", "--skip-plan-folder-dedupe"],
)
def test_export_rejects_unshadowed_additive_overrides(
    tmp_path: Path,
    unsafe_flag: str,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    backlog_path = tmp_path / "runs" / "target_a" / "_compiled" / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target_a", "repo_input": None},
            "tickets": [],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                unsafe_flag,
            ]
        )

    assert exc.value.code == 2


@pytest.mark.parametrize("drift_kind", ["policy", "ux_review"])
def test_export_rejects_bound_input_drift_before_mutation(
    tmp_path: Path,
    drift_kind: str,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    backlog_path = tmp_path / "custom" / "historical.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target_a", "repo_input": None},
            "tickets": [],
        },
    )
    _bind_shadow_export_contract(
        repo_root=repo_root,
        backlog_path=backlog_path,
        tmp_path=tmp_path,
        generated_at="2026-07-09T00:00:00Z",
    )
    if drift_kind == "policy":
        policy_path = repo_root / "configs" / "backlog_policy.yaml"
        policy_path.write_text(
            policy_path.read_text(encoding="utf-8") + "\n# changed after shadow\n",
            encoding="utf-8",
        )
    else:
        _write_json(
            export_commands._ux_review_path_for_backlog(backlog_path),
            {"schema_version": 1, "status": "ok", "review": {"recommendations": []}},
        )
    atom_actions_path = tmp_path / "atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--backlog-json",
                str(backlog_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )

    assert exc.value.code == 2
    assert not atom_actions_path.exists()
    assert not (
        tmp_path / "runs" / "target_a" / "_compiled" / "target_a.tickets_export.json"
    ).exists()


def test_export_detects_backlog_swap_between_validation_and_first_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    backlog_path = tmp_path / "runs" / "target_a" / "_compiled" / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target_a", "repo_input": None},
            "tickets": [],
        },
    )
    _bind_shadow_export_contract(
        repo_root=repo_root,
        backlog_path=backlog_path,
        tmp_path=tmp_path,
        generated_at="2026-07-09T00:00:00Z",
    )
    validated = export_commands.validate_shadow_export_state
    validation_calls = 0

    def validate_then_swap(**kwargs: object) -> tuple[bool, list[str], dict[str, object] | None]:
        nonlocal validation_calls
        result = validated(**kwargs)  # type: ignore[arg-type]
        validation_calls += 1
        if validation_calls == 1:
            swapped = json.loads(backlog_path.read_text(encoding="utf-8"))
            swapped["swap_marker"] = "changed-after-validation"
            _write_json(backlog_path, swapped)
        return result

    monkeypatch.setattr(
        export_commands,
        "validate_shadow_export_state",
        validate_then_swap,
    )
    atom_actions_path = tmp_path / "atom_actions.yaml"

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--backlog-json",
                str(backlog_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )

    assert exc.value.code == 2
    assert validation_calls == 1
    assert not atom_actions_path.exists()


def test_late_invalid_ticket_leaves_all_scoped_files_byte_identical(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    owner_repo = tmp_path / "owner"
    owner_repo.mkdir()
    first: dict[str, object] = {
        "ticket_id": "BLG-ATOMIC-FIRST",
        "title": "First ticket would refresh an existing plan",
        "problem": "The first item must not mutate before the batch is valid.",
        "severity": "high",
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target/run/agent/0:failure:1"],
        "change_surface": {"user_visible": False, "kinds": ["behavior_change"]},
        "suggested_owner": "docs",
    }
    second: dict[str, object] = {
        "ticket_id": "BLG-ATOMIC-SECOND",
        "title": "Second ticket has an invalid outcome",
        "problem": "A late outcome conflict must reject the complete batch.",
        "severity": "high",
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target/run/agent/0:failure:2"],
        "change_surface": {"user_visible": False, "kinds": ["behavior_change"]},
        "suggested_owner": "docs",
    }
    _with_strict_readiness(first)
    _with_strict_readiness(second)
    first_fingerprint = ticket_export_fingerprint(first)
    second_fingerprint = ticket_export_fingerprint(second)
    legacy_plan = (
        owner_repo
        / ".agents"
        / "plans"
        / "2 - ready"
        / f"20260709_TKT-123456789abc_{first_fingerprint}_legacy.md"
    )
    legacy_plan.parent.mkdir(parents=True)
    legacy_plan.write_text(
        "\n".join(
            [
                "# Legacy existing plan",
                "",
                f"- Fingerprint: `{first_fingerprint}`",
                "- Source ticket: `TKT-123456789abc`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    backlog_path = tmp_path / "runs" / "target" / "_compiled" / "target.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target", "repo_input": str(owner_repo)},
            "tickets": [first, second],
        },
    )
    actions_path = tmp_path / "actions.yaml"
    _write_yaml(
        actions_path,
        {
            "version": 1,
            "actions": [
                {
                    "fingerprint": second_fingerprint,
                    "status": "actioned",
                    "outcome": "not-an-outcome-object",
                }
            ],
        },
    )
    atom_actions_path = tmp_path / "atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {"atom_id": "target/run/agent/0:failure:1", "status": "new"},
                {"atom_id": "target/run/agent/0:failure:2", "status": "new"},
            ],
        },
    )
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )

    assert exc.value.code == 2
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert legacy_plan.exists()
    assert not legacy_plan.with_name(f"20260709_{first_fingerprint}_legacy.md").exists()
    assert not (backlog_path.parent / "target.tickets_export.json").exists()


def test_export_demotes_ready_ticket_when_retained_research_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    owner_repo = tmp_path / "owner"
    owner_repo.mkdir()
    compiled = tmp_path / "runs" / "target" / "_compiled"
    ticket: dict[str, object] = {
        "ticket_id": "BLG-EVIDENCE",
        "title": "Retained evidence changed",
        "problem": "The original proof is no longer reproducible.",
        "severity": "high",
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target/run/agent/0:failure:1"],
        "change_surface": {"user_visible": False, "kinds": ["behavior_change"]},
        "suggested_owner": "core",
    }
    _with_strict_readiness(ticket)
    research = ticket["research"]
    assert isinstance(research, dict)
    _SYNTHETIC_RESEARCH_IDS.discard(
        (str(research.get("case_id") or ""), str(research.get("problem_id") or ""))
    )
    _write_json(
        compiled / "target.backlog.json",
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    monkeypatch.setattr(
        export_commands,
        "verify_persisted_research_evidence",
        lambda _dossier: (False, ["research_artifact_changed:artifact:source"]),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target",
            ]
        )

    assert exc.value.code == 0
    exported = json.loads((compiled / "target.tickets_export.json").read_text(encoding="utf-8"))
    assert exported["exports"][0]["export_kind"] == "research"
    readiness = exported["exports"][0]["source_ticket"]["ticket_readiness"]
    assert readiness["ready"] is False
    assert "retained_research_evidence_invalid" in readiness["reasons"]


def test_bound_export_locks_when_retained_research_changes_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    ticket: dict[str, object] = {
        "ticket_id": "BLG-EVIDENCE-BOUND",
        "title": "Retained evidence must remain bound",
        "problem": "A previously verified mechanism can drift after shadowing.",
        "severity": "high",
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target/run/agent/0:failure:1"],
        "change_surface": {"user_visible": False, "kinds": ["behavior_change"]},
        "suggested_owner": "runner_core",
    }
    _with_strict_readiness(ticket)
    backlog_path = tmp_path / "runs" / "target" / "_compiled" / "target.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target", "repo_input": None},
            "tickets": [ticket],
        },
    )
    _bind_shadow_export_contract(
        repo_root=repo_root,
        backlog_path=backlog_path,
        tmp_path=tmp_path,
        generated_at="2026-07-09T00:00:00Z",
    )
    monkeypatch.setattr(
        export_commands,
        "verify_persisted_research_evidence",
        lambda _dossier: (False, ["research_artifact_changed:artifact:source"]),
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target",
            ]
        )

    assert exc.value.code == 2
    assert not (tmp_path / "runs" / "target" / "_compiled" / "target.tickets_export.json").exists()
    assert not (repo_root / ".agents" / "plans").exists()


def test_bound_export_locks_when_owner_context_changes(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    owner_repo = tmp_path / "owner"
    owner_repo.mkdir()
    ticket: dict[str, object] = {
        "ticket_id": "BLG-OWNER-BOUND",
        "title": "Owner route must remain bound",
        "problem": "Repository routing context can change after shadowing.",
        "severity": "high",
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target/run/agent/0:failure:1"],
        "change_surface": {"user_visible": False, "kinds": ["behavior_change"]},
        "suggested_owner": "docs",
    }
    _with_strict_readiness(ticket)
    backlog_path = tmp_path / "runs" / "target" / "_compiled" / "target.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target", "repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _bind_shadow_export_contract(
        repo_root=repo_root,
        backlog_path=backlog_path,
        tmp_path=tmp_path,
        generated_at="2026-07-09T00:00:00Z",
    )
    git_config = owner_repo / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text('[remote "origin"]\nurl = https://example.test/changed.git\n')

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target",
            ]
        )

    assert exc.value.code == 2
    assert not (owner_repo / ".agents" / "plans").exists()


def _with_strict_readiness(
    ticket: dict[str, object], *, needs_ux_review: bool = False
) -> dict[str, object]:
    """Attach a minimal but fully evidence-linked stage 1-6 chain."""

    pid = f"problem:{ticket.get('ticket_id', 'test')}"
    case_id = f"case:{ticket.get('ticket_id', 'test')}"
    option_id = f"option:{ticket.get('ticket_id', 'test')}:direct"
    coverage = {
        "mechanism_addressed": "A traced local decision omits the required guard",
        "research_binding": {
            "hypothesis_id": "h1",
            "hypothesis_statement": "The local decision omits a guard",
            "mechanism_symbols": ["core.run"],
            "supporting_evidence_refs": ["exp-1", "exp-challenge"],
            "counterevidence_refs": ["exp-control"],
            "falsification_attempt_refs": ["falsify-h1-alternative"],
            "deterministic_closure_refs": [],
            "intervention_points": [
                {
                    "mechanism_symbol": "core.run",
                    "target_path": "src/core.py",
                    "target_symbol": "core.run",
                    "intervention": "Apply the guard at the verified local decision.",
                }
            ],
        },
        "symptoms_covered": [str(ticket.get("problem") or "Observed failure")],
        "unsupported_assumptions": [],
        "residual_recurrence_paths": [],
        "compatibility_risks": [],
        "testability": {"before": "Focused test fails", "after": "Focused test passes"},
        "outcome_strategy": {
            "intended_operation": "The focused operation completes with the required guard.",
            "success_properties": [
                "The retained original replay reports that the guard was applied."
            ],
            "safety_constraints": ["The existing successful path remains unchanged."],
            "original_scenario_experiment_ids": ["exp-1"],
        },
    }
    option = {
        "case_id": case_id,
        "option_id": option_id,
        "problem_id": pid,
        "family_id": "most_direct",
        "summary": "Apply the traced local guard",
        "tradeoffs": "Keeps the change local",
        "recurrence_prevention": "Focused regression covers the decision",
        "change_surface_hypothesis": "Existing surface behavior",
        "test_implications": "Replay exp-1",
        "rationale": "The static trace identifies this decision",
        "causal_coverage": coverage,
        "scope_evidence": {
            "scope_level": "single_path",
            "independent_consumers_or_failure_paths": [
                {"name": "core.run", "evidence_refs": ["exp-1"]}
            ],
        },
    }
    selection_surface = ticket.get("change_surface")
    if not isinstance(selection_surface, dict) or not selection_surface.get("kinds"):
        selection_surface = {
            "user_visible": False,
            "kinds": ["behavior_change"],
            "notes": "Internal behavior",
        }
    selection = {
        "case_id": case_id,
        "problem_id": pid,
        "selected_option_id": option_id,
        "selected_family_id": "most_direct",
        "selection_rationale": "Matches the traced mechanism",
        "repo_intent_alignment": "Uses the existing surface",
        "why_other_options_were_not_selected": "No broader mechanism is evidenced",
        "needs_ux_review": needs_ux_review,
        "causal_coverage_evaluation": {
            "mechanism_fit": "Direct",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": False,
        },
        "falsification_review": {
            "problem_id": pid,
            "selected_option_id": option_id,
            "verdict": "accept",
            "strongest_counterargument": "A different decision may be responsible",
            "evidence_refs": [
                {
                    "ref": "exp-1",
                    "finding": "The local decision omits the guard",
                    "effect": "challenges_selection",
                }
            ],
            "unsupported_assumptions": [],
            "residual_risks": [],
            "evidence_that_would_change_verdict": "A contrary trace",
            "material_risk_dispositions": [],
            "critical_findings": [],
            "outcome_strategy_review": {
                "verdict": "sufficient",
                "semantic_relation_assessment": (
                    "The strategy requires the useful guarded operation on the retained replay."
                ),
                "proves_intended_operation": True,
                "problem_coverage": "full",
                "residual_untested_paths": [],
                "evidence_refs": ["exp-1"],
            },
        },
        "change_surface": selection_surface,
    }
    research = {
        "research_schema_version": 3,
        "case_id": case_id,
        "problem_id": pid,
        "repo_revision": "abc123",
        "research_method": "static_trace",
        "reproduction_status": "reproduction_failed",
        "research_status": "evidence_sufficient",
        "writes_used": False,
        "writes_purpose": ["static_trace"],
        "implementation_performed": False,
        "diff_classification": "no_changes",
        "artifact_refs": [
            {
                "artifact_id": "artifact:source",
                "kind": "source",
                "path": "src/core.py",
            }
        ],
        "experiments": [
            {
                "experiment_id": "exp-1",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:test"],
                "command": ("python -m pytest -q tests/test_core.py::test_missing_guard"),
                "result": "Trace establishes the missing guard",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "origin_evidence_bindings": [
                    {
                        "atom_id": "atom:test",
                        "role": "expected_behavior",
                        "field_path": "$.expected_output",
                        "value": "guard applied",
                        "value_sha256": sha256(
                            json.dumps(
                                "guard applied",
                                sort_keys=True,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest(),
                    }
                ],
                "positive_outcome_contract": {
                    "contract_kind": "origin_atom_exact_value",
                    "atom_id": "atom:test",
                    "field_path": "$.expected_output",
                    "postcondition": {
                        "type": "command_stdout_contains",
                        "value": "guard applied",
                    },
                },
                "artifact_refs": ["artifact:source"],
            },
            {
                "experiment_id": "exp-control",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-1",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "guard enabled",
                    "expected_difference": "The guarded control succeeds without the symptom.",
                },
                "addresses_atom_ids": ["atom:test"],
                "command": ("python -m pytest -q tests/test_core.py::test_guarded_control"),
                "result": "The guarded path succeeds",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": ["artifact:source"],
            },
            {
                "experiment_id": "exp-challenge",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "exp-1",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "the strongest alternative cause",
                    "expected_difference": (
                        "The failure disappears only if the alternative is causal."
                    ),
                },
                "addresses_atom_ids": ["atom:test"],
                "command": ("python -m pytest -q tests/test_core.py::test_alternative_removed"),
                "result": "The original failure remains",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": ["artifact:source"],
            },
        ],
        "inspected_files": ["src/core.py"],
        "inspected_symbols": ["core.run"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": "The local decision omits a guard",
                "supporting_evidence": ["exp-1", "exp-challenge"],
                "counterevidence": ["exp-control"],
                "falsification_attempts": [
                    {
                        "attempt_id": "falsify-h1-alternative",
                        "hypothesis_id": "h1",
                        "claim": "The local decision omits a guard",
                        "baseline_experiment_id": "exp-1",
                        "challenge_experiment_id": "exp-challenge",
                        "disproof_condition": {
                            "source": "exit_code",
                            "operator": "equals",
                            "expected": 0,
                        },
                        "outcome": "survived",
                    }
                ],
                "mechanism_symbols": ["core.run"],
                "disposition": "primary",
                "disposition_evidence": ["exp-1", "exp-control"],
            }
        ],
        "root_cause_confidence": 0.9,
        "broader_class_assessment": "isolated_instance",
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [],
        "case_relation_assessment": {
            "disposition": "retain",
            "rationale": (
                "The signed occurrence and traced local mechanism remain one work unit."
            ),
            "facets": [],
            "material_unknowns": [],
        },
    }
    _SYNTHETIC_RESEARCH_IDS.add((case_id, pid))
    assignment = {
        "status": "complete",
        "errors": [],
        "case_id": case_id,
        "problem_id": pid,
        "expected_atom_ids": ["atom:test"],
        "atom_receipts": [
            {
                "atom_id": "atom:test",
                "atom_sha256": sha256(
                    json.dumps(
                        {
                            "atom_id": "atom:test",
                            "text": "failure",
                            "command": (
                                "python -m pytest -q tests/test_core.py::test_missing_guard"
                            ),
                            "exit_code": 1,
                            "evidence_role": "observation",
                            "origin_stage": "runtime",
                            "expected_output": "guard applied",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "atom_snapshot": {
                    "atom_id": "atom:test",
                    "text": "failure",
                    "command": ("python -m pytest -q tests/test_core.py::test_missing_guard"),
                    "exit_code": 1,
                    "evidence_role": "observation",
                    "origin_stage": "runtime",
                    "expected_output": "guard applied",
                },
                "artifact_receipts": [
                    {"path": "C:/runs/origin.json", "sha256": "5" * 64, "size_bytes": 7}
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    research["evidence_assignment"] = assignment
    isolation = {
        "executor": "trusted_host",
        "os_sandbox": False,
        "network": "not_enforced",
        "filesystem_isolation": "dedicated_clone_only_not_os_sandbox",
        "trust_decision": "approved_local_source_root",
        "trust_reason": "C:/runs/source",
        "source_workspace": "C:/runs/research-workspace",
        "sanitized_environment_keys": ["CI"],
    }
    verification = {
        "verification_method": "runner_artifact_binding_v1",
        "status": "verified",
        "case_id": case_id,
        "problem_id": pid,
        "repo_revision": "abc123",
        "requested_repo_ref": "origin/dev",
        "resolved_repo_ref": "abc123",
        "workspace_dir": "C:/runs/research-workspace",
        "workspace_head": "abc123",
        "workspace_overlay": {
            "baseline_manifest_sha256": "6" * 64,
            "research_manifest_sha256": "7" * 64,
            "baseline_state_sha256": "8" * 64,
            "research_state_sha256": "9" * 64,
            "baseline_git_index_sha256": "a" * 64,
            "research_git_index_sha256": "b" * 64,
            "changed_baseline_paths": [],
            "research_overlay_paths": [".usertest_research/repro.txt"],
            "research_overlay_manifest": {
                ".usertest_research/repro.txt": {
                    "kind": "file",
                    "mode": 420,
                    "sha256": "c" * 64,
                    "size_bytes": 12,
                }
            },
            "research_overlay_manifest_sha256": "d" * 64,
            "suspicious_extra_paths": [],
            "git_index_changed": False,
        },
        "replay_isolation": isolation,
        "planning_workspace_dir": "C:/runs/planning-workspace",
        "planning_workspace_head": "abc123",
        "planning_workspace_clean": True,
        "run_dir": "C:/runs/research",
        "origin_atom_ids": ["atom:test"],
        "assignment_sha256": assignment["assignment_sha256"],
        "claims_sha256": research_claims_sha256(research),
        "normalized_events_sha256": "a" * 64,
        "run_report_sha256": "e" * 64,
        "artifacts": [
            {
                "artifact_id": "artifact:source",
                "kind": "source",
                "path": "src/core.py",
                "sha256": "b" * 64,
                "size_bytes": 42,
            }
        ],
        "experiments": [
            {
                "experiment_id": experiment["experiment_id"],
                "command": experiment["command"],
                "executed_argv": experiment["command"].split(),
                "exit_code": experiment["exit_code"],
                "event_index": index,
                "agent_event_index": index,
                "agent_event_sha256": "c" * 64,
                "agent_output_excerpt_sha256": None,
                "scenario_kind": experiment["scenario_kind"],
                "addresses_atom_ids": experiment["addresses_atom_ids"],
                "declared_result": experiment["result"],
                "outcome": experiment["outcome"],
                "workspace_dir": f"C:/runs/replay-{index}",
                "workspace_head": research["repo_revision"],
                "baseline_state_sha256": "4" * 64,
                "pre_replay_state_sha256": "5" * 64,
                "post_replay_state_sha256": "5" * 64,
                "post_replay_mutations": False,
                "overlay_manifest_sha256": "d" * 64,
                "execution_isolation": isolation,
                "execution_metadata": {
                    "executor": "trusted_host",
                    "os_sandbox": False,
                    "network": "not_enforced",
                },
                "stdout_path": f"C:/runs/replay-{index}/stdout.txt",
                "stderr_path": f"C:/runs/replay-{index}/stderr.txt",
                "stdout_sha256": "f" * 64,
                "stderr_sha256": "1" * 64,
                "observable_assertion": experiment["observable_assertion"],
                "assertion_passed": True,
                "artifact_refs": experiment["artifact_refs"],
            }
            for index, experiment in enumerate(research["experiments"])
        ],
        "inspected_files": [
            {
                "path": "src/core.py",
                "sha256": "d" * 64,
                "git_blob_sha": "2" * 40,
                "size_bytes": 42,
                "read_event_index": 2,
                "read_event_sha256": "3" * 64,
                "read_source": "tool",
                "bytes_observed": 42,
                "whole_file_observed": True,
                "observed_content_sha256": "4" * 64,
                "observed_start_line": 1,
                "observed_end_line": 3,
            }
        ],
        "inspected_symbols": [{"symbol": "core.run", "path": "src/core.py"}],
        "hypothesis_refs": [
            {
                "hypothesis_id": "h1",
                "supporting_refs": ["exp-1", "exp-challenge"],
                "counterevidence_refs": ["exp-control"],
                "mechanism_symbols": ["core.run"],
                "disposition": "primary",
                "disposition_evidence_refs": ["exp-1", "exp-control"],
                "control_links": [
                    {
                        "control_experiment_id": "exp-control",
                        "supports_experiment_id": "exp-1",
                        "mechanism_symbols": ["core.run"],
                        "shared_atom_ids": ["atom:test"],
                        "shared_artifact_refs": ["artifact:source"],
                        "controlled_variable": "guard enabled",
                        "expected_difference": (
                            "The guarded control succeeds without the symptom."
                        ),
                    }
                ],
            }
        ],
        "causal_links": [
            {
                "hypothesis_id": "h1",
                "experiment_id": "exp-1",
                "symbol": "core.run",
                "path": "src/core.py",
                "stream": "stderr",
                "trace_kind": "python_traceback",
                "trace_excerpt_sha256": "8" * 64,
                "stream_sha256": "1" * 64,
            }
        ],
        "test_selections": [
            {
                "selection_id": "h1:exp-1",
                "hypothesis_id": "h1",
                "experiment_id": "exp-1",
                "runner": "pytest",
                "command_sha256": sha256(
                    str(research["experiments"][0]["command"]).encode()
                ).hexdigest(),
                "executed_argv_sha256": sha256(
                    json.dumps(
                        str(research["experiments"][0]["command"]).split(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "test_path": "tests/test_core.py",
                "test_file_sha256": "7" * 64,
                "test_file_git_blob_sha": "2" * 40,
                "selector": "test_missing_guard",
                "selector_parts": ["test_missing_guard"],
                "test_function": "test_missing_guard",
                "test_function_line": 5,
                "test_function_source_sha256": "8" * 64,
                "reachable_functions": ["test_missing_guard"],
                "mechanism_touches": [
                    {
                        "symbol": "core.run",
                        "source_path": "src/core.py",
                        "calls": [
                            {
                                "function": "test_missing_guard",
                                "line": 6,
                                "expression": "run",
                                "resolved_target": "core.run",
                            }
                        ],
                    }
                ],
            },
            {
                "selection_id": "h1:exp-control",
                "hypothesis_id": "h1",
                "experiment_id": "exp-control",
                "runner": "pytest",
                "command_sha256": sha256(
                    str(research["experiments"][1]["command"]).encode()
                ).hexdigest(),
                "executed_argv_sha256": sha256(
                    json.dumps(
                        str(research["experiments"][1]["command"]).split(),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "test_path": "tests/test_core.py",
                "test_file_sha256": "7" * 64,
                "test_file_git_blob_sha": "2" * 40,
                "selector": "test_guarded_control",
                "selector_parts": ["test_guarded_control"],
                "test_function": "test_guarded_control",
                "test_function_line": 10,
                "test_function_source_sha256": "9" * 64,
                "reachable_functions": ["test_guarded_control"],
                "mechanism_touches": [
                    {
                        "symbol": "core.run",
                        "source_path": "src/core.py",
                        "calls": [
                            {
                                "function": "test_guarded_control",
                                "line": 11,
                                "expression": "run",
                                "resolved_target": "core.run",
                            }
                        ],
                    }
                ],
            },
        ],
        "control_verifications": [
            {
                "verification_method": "pytest_ast_mechanism_call_v1",
                "hypothesis_id": "h1",
                "support_experiment_id": "exp-1",
                "control_experiment_id": "exp-control",
                "support_selection_id": "h1:exp-1",
                "control_selection_id": "h1:exp-control",
                "mechanism_symbols": ["core.run"],
                "shared_verified_mechanism_symbols": ["core.run"],
                "same_test_file": True,
                "relationship_sha256": sha256(
                    json.dumps(
                        {
                            "controlled_variable": "guard enabled",
                            "expected_difference": (
                                "The guarded control succeeds without the symptom."
                            ),
                            "mechanism_symbols": ["core.run"],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
        ],
        "atom_bindings": [
            {
                "experiment_id": "exp-1",
                "atom_id": "atom:test",
                "match_kind": "command_and_exit_code",
                "origin_atom_sha256": assignment["atom_receipts"][0][
                    "atom_sha256"
                ],
            }
        ],
        "errors": [],
    }
    verification["atom_bindings"].append(
        {
            "experiment_id": "exp-1",
            "atom_id": "atom:test",
            "binding_role": "expected_behavior",
            "match_kind": "explicit_field_binding",
            "origin_atom_sha256": assignment["atom_receipts"][0]["atom_sha256"],
            "origin_atom_field_path": "$.expected_output",
            "origin_atom_value_sha256": sha256(
                json.dumps(
                    "guard applied",
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
    )
    support_selection = verification["test_selections"][0]
    control_selection = verification["test_selections"][1]
    support_call = support_selection["mechanism_touches"][0]["calls"][0]
    control_call = control_selection["mechanism_touches"][0]["calls"][0]
    support_call.update({"arguments": [], "arguments_complete": True})
    control_argument = {
        "slot": "keyword:guarded",
        "expression": "True",
        "ast_sha256": sha256(b"Constant(value=True)").hexdigest(),
    }
    control_call.update({"arguments": [control_argument], "arguments_complete": True})
    old_control = verification["control_verifications"][0]
    control_receipt = {
        "verification_method": "pytest_ast_controlled_difference_v2",
        "hypothesis_id": "h1",
        "support_experiment_id": "exp-1",
        "control_experiment_id": "exp-control",
        "support_selection_id": "h1:exp-1",
        "control_selection_id": "h1:exp-control",
        "mechanism_symbols": ["core.run"],
        "shared_verified_mechanism_symbols": ["core.run"],
        "same_test_file": True,
        "controlled_input_difference": {
            "verification_method": "python_ast_explicit_argument_delta_v1",
            "difference_count": 1,
            "difference": {
                "mechanism_symbol": "core.run",
                "slot": "keyword:guarded",
                "difference_kind": "added_in_control",
                "support_argument": None,
                "control_argument": control_argument,
            },
        },
        "observable_difference": {
            "verification_method": "runner_replay_complement_v1",
            "source": "exit_code",
            "difference_kind": "failing_exit_to_zero",
            "expected_sha256": None,
            "support": {
                "exit_code": 1,
                "observed_sha256": sha256(b"1").hexdigest(),
                "stdout_sha256": "f" * 64,
                "stderr_sha256": "1" * 64,
            },
            "control": {
                "exit_code": 0,
                "observed_sha256": sha256(b"0").hexdigest(),
                "stdout_sha256": "f" * 64,
                "stderr_sha256": "1" * 64,
            },
        },
        "adversarial_effect": "limits_scope",
        "relationship_sha256": old_control["relationship_sha256"],
    }
    control_receipt["control_verification_id"] = (
        "control_verification:"
        + sha256(
            json.dumps(
                control_receipt,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    verification["control_verifications"] = [control_receipt]
    consumer_projection = {
        "kind": "runner_observed_entrypoint",
        "entrypoint": "core.run",
        "attestation_basis": "runner_mechanism_link",
        "runner_attested": True,
    }
    consumer_identity = {
        **consumer_projection,
        "consumer_identity_sha256": sha256(
            json.dumps(
                consumer_projection,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    consumer_independence_key = sha256(
        json.dumps(
            consumer_identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    mechanism_evidence = {
        "evidence_type": "controlled_scenario",
        "hypothesis_id": "h1",
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
        "experiment_ids": ["exp-1", "exp-control"],
        "artifact_refs": ["artifact:source"],
        "origin_atom_ids": ["atom:test"],
        "origin_symptom_bindings": [
            {
                "experiment_id": "exp-1",
                "atom_id": "atom:test",
                "match_kind": "command_and_exit_code",
                "origin_atom_sha256": assignment["atom_receipts"][0][
                    "atom_sha256"
                ],
            }
        ],
        "path_name": "core.run",
        "consumer_identity": consumer_identity,
        "independence_key": consumer_independence_key,
        "controlled_condition": {
            "variable": "guarded",
            "expected_difference": "The guarded control removes the failure.",
        },
        "observable_difference": control_receipt["observable_difference"],
        "strong_pytest_control_id": control_receipt["control_verification_id"],
        "mechanism_link": {
            "verification_method": "runner_exception_symbol_trace_v1",
            "entrypoint": "core.run",
            "code_path": [
                {
                    "symbol": "core.run",
                    "path": "src/core.py",
                    "trace_excerpt_sha256": "8" * 64,
                }
            ],
        },
        "adversarial_effect": "supports_selection",
    }
    mechanism_evidence["causal_root_bindings"] = [
        {
            "kind": "origin_symptom_observation",
            "experiment_ids": ["exp-1", "exp-control"],
            "origin_atom_ids": ["atom:test"],
            "origin_bindings_sha256": sha256(
                json.dumps(
                    mechanism_evidence["origin_symptom_bindings"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "mechanism_link_sha256": sha256(
                json.dumps(
                    mechanism_evidence["mechanism_link"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            "root_mechanism_symbol": "core.run",
        }
    ]
    mechanism_evidence["mechanism_evidence_id"] = (
        "mechanism_evidence:"
        + sha256(
            json.dumps(
                mechanism_evidence,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    challenge_evidence = {
        "evidence_type": "exception_trace",
        "hypothesis_id": "h1",
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
        "experiment_ids": ["exp-challenge"],
        "artifact_refs": ["artifact:source"],
        "origin_atom_ids": ["atom:test"],
        "origin_symptom_bindings": [],
        "path_name": "core.run",
        "consumer_identity": consumer_identity,
        "independence_key": consumer_independence_key,
        "observed_result": {
            "exit_code": 1,
            "stdout_sha256": "f" * 64,
            "stderr_sha256": "1" * 64,
            "assertion": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 1,
            },
        },
        "harness_path": None,
        "mechanism_link": {
            "verification_method": "runner_exception_symbol_trace_v1",
            "entrypoint": "core.run",
            "code_path": [
                {
                    "symbol": "core.run",
                    "path": "src/core.py",
                    "trace_excerpt_sha256": "8" * 64,
                }
            ],
        },
        "platform_requirement": "any",
        "observed_platform": "windows",
        "adversarial_effect": "supports_selection",
    }
    challenge_evidence["mechanism_evidence_id"] = (
        "mechanism_evidence:"
        + sha256(
            json.dumps(
                challenge_evidence,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    verification["mechanism_evidence"] = [
        mechanism_evidence,
        challenge_evidence,
    ]
    replay_by_id = {replay["experiment_id"]: replay for replay in verification["experiments"]}
    alternative_argument = {
        "slot": "keyword:alternative",
        "expression": "False",
        "ast_sha256": sha256(b"Constant(value=False)").hexdigest(),
    }
    intervention = {
        "verification_method": "pytest_ast_falsification_intervention_v1",
        "hypothesis_id": "h1",
        "attempt_id": "falsify-h1-alternative",
        "baseline_experiment_id": "exp-1",
        "challenge_experiment_id": "exp-challenge",
        "mechanism_symbols": ["core.run"],
        "baseline_selection_id": "h1:exp-1",
        "challenge_selection_id": "h1:exp-challenge",
        "controlled_input_difference": {
            "verification_method": "python_ast_explicit_argument_delta_v1",
            "difference_count": 1,
            "difference": {
                "mechanism_symbol": "core.run",
                "slot": "keyword:alternative",
                "difference_kind": "added_in_control",
                "support_argument": None,
                "control_argument": alternative_argument,
            },
        },
        "observed_polarity": {
            "verification_method": "runner_replay_falsification_polarity_v1",
            "polarity": "failure_persists_after_intervention",
            "baseline": {
                "exit_code": replay_by_id["exp-1"]["exit_code"],
                "stdout_sha256": replay_by_id["exp-1"]["stdout_sha256"],
                "stderr_sha256": replay_by_id["exp-1"]["stderr_sha256"],
            },
            "challenge": {
                "exit_code": replay_by_id["exp-challenge"]["exit_code"],
                "stdout_sha256": replay_by_id["exp-challenge"]["stdout_sha256"],
                "stderr_sha256": replay_by_id["exp-challenge"]["stderr_sha256"],
            },
        },
        "relationship_sha256": sha256(
            json.dumps(
                {
                    "controlled_variable": "the strongest alternative cause",
                    "expected_difference": (
                        "The failure disappears only if the alternative is causal."
                    ),
                    "mechanism_symbols": ["core.run"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    intervention["intervention_receipt_id"] = (
        "falsification_intervention:"
        + sha256(
            json.dumps(
                intervention,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    verification["falsification_interventions"] = [intervention]
    verification["deterministic_mechanism_closures"] = []
    verification["hypothesis_refs"][0]["falsification_attempts"] = [
        {
            "attempt_id": "falsify-h1-alternative",
            "hypothesis_id": "h1",
            "claim": "The local decision omits a guard",
            "baseline_experiment_id": "exp-1",
            "challenge_experiment_id": "exp-challenge",
            "disproof_condition": {
                "source": "exit_code",
                "operator": "equals",
                "expected": 0,
            },
            "outcome": "survived",
            "scenario_kind": "control",
            "command": research["experiments"][2]["command"],
            "declared_result": research["experiments"][2]["result"],
            "observable_assertion": research["experiments"][2]["observable_assertion"],
            "exit_code": replay_by_id["exp-challenge"]["exit_code"],
            "stdout_sha256": replay_by_id["exp-challenge"]["stdout_sha256"],
            "stderr_sha256": replay_by_id["exp-challenge"]["stderr_sha256"],
            "mechanism_evidence_ids": [challenge_evidence["mechanism_evidence_id"]],
            "intervention_receipt_id": intervention["intervention_receipt_id"],
        }
    ]
    verification["verified_mechanism"] = {
        "schema_version": 3,
        "mechanism_symbols": ["core.run"],
        "code_paths": [{"symbol": "core.run", "path": "src/core.py"}],
    }
    probe_points = [
        {
            "verification_method": receipt["controlled_input_difference"]["verification_method"],
            "mechanism_symbols": ["core.run"],
            "slot": receipt["controlled_input_difference"]["difference"]["slot"],
            "mechanism_symbol": "core.run",
        }
        for receipt in (control_receipt, intervention)
    ]
    verification["verified_mechanism_provenance"] = {
        "schema_version": 2,
        "primary_hypothesis_id": "h1",
        "mechanism_evidence_ids": sorted(
            [
                mechanism_evidence["mechanism_evidence_id"],
                challenge_evidence["mechanism_evidence_id"],
            ]
        ),
        "causal_root_evidence_ids": [
            mechanism_evidence["mechanism_evidence_id"]
        ],
        "support_connectivity": sorted(
            [
                {
                    "mechanism_evidence_id": mechanism_evidence[
                        "mechanism_evidence_id"
                    ],
                    "experiment_ids": ["exp-1", "exp-control"],
                    "connection_kind": "causal_root",
                    "connected_from_mechanism_evidence_id": None,
                    "shared_verified_symbols": [],
                    "verified_causal_edge": None,
                    "verified_causal_edges": [],
                    "causal_root_kinds": ["origin_symptom_observation"],
                },
                {
                    "mechanism_evidence_id": challenge_evidence[
                        "mechanism_evidence_id"
                    ],
                    "experiment_ids": ["exp-challenge"],
                    "connection_kind": "shared_verified_symbol",
                    "connected_from_mechanism_evidence_id": mechanism_evidence[
                        "mechanism_evidence_id"
                    ],
                    "shared_verified_symbols": ["core.run"],
                    "verified_causal_edge": None,
                    "verified_causal_edges": [],
                    "causal_root_kinds": [],
                },
            ],
            key=lambda value: value["mechanism_evidence_id"],
        ),
        "support_symbol_coverage": sorted(
            [
                {
                    "experiment_ids": ["exp-1", "exp-control"],
                    "mechanism_symbols": ["core.run"],
                },
                {
                    "experiment_ids": ["exp-challenge"],
                    "mechanism_symbols": ["core.run"],
                },
            ],
            key=lambda value: json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
        "causal_control_ids": [control_receipt["control_verification_id"]],
        "falsification_intervention_ids": [intervention["intervention_receipt_id"]],
        "deterministic_closure_ids": [],
        "research_probe_control_points": sorted(
            probe_points,
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        ),
    }
    verification["verified_mechanism_sha256"] = sha256(
        json.dumps(
            verification["verified_mechanism"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    verification["verified_mechanism_provenance_sha256"] = sha256(
        json.dumps(
            verification["verified_mechanism_provenance"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    oracle = {
        "schema_version": 1,
        "case_id": case_id,
        "repo_revision": "abc123",
        "primary_hypothesis_id": "h1",
        "primary_verified_mechanism_sha256": verification[
            "verified_mechanism_sha256"
        ],
        "primary_verified_mechanism_provenance_sha256": verification[
            "verified_mechanism_provenance_sha256"
        ],
        "research_experiment_id": "exp-1",
        "scenario_kind": "original_replay",
        "origin_atom_ids": ["atom:test"],
        "mechanism_evidence_ids": [mechanism_evidence["mechanism_evidence_id"]],
        "baseline": {
            "exit_code": 1,
            "observable_assertion": research["experiments"][0]["observable_assertion"],
            "stdout_sha256": replay_by_id["exp-1"]["stdout_sha256"],
            "stderr_sha256": replay_by_id["exp-1"]["stderr_sha256"],
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": replay_by_id["exp-1"]["executed_argv"],
            "command_authorization": {
                "authorization_kind": "standard_test_or_research_harness",
                "executed_argv_sha256": sha256(
                    json.dumps(
                        replay_by_id["exp-1"]["executed_argv"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
    }
    positive_contract = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "primary_hypothesis_id": "h1",
        "primary_verified_mechanism_sha256": verification[
            "verified_mechanism_sha256"
        ],
        "primary_verified_mechanism_provenance_sha256": verification[
            "verified_mechanism_provenance_sha256"
        ],
        "research_experiment_id": "exp-1",
        "mechanism_evidence_ids": [mechanism_evidence["mechanism_evidence_id"]],
        "origin_evidence": {
            "atom_id": "atom:test",
            "atom_sha256": assignment["atom_receipts"][0]["atom_sha256"],
            "field_path": "$.expected_output",
            "value_sha256": sha256(
                json.dumps(
                    "guard applied",
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "guard applied",
            },
        ],
    }
    positive_contract["positive_outcome_contract_id"] = (
        "positive_outcome_contract:"
        + sha256(
            json.dumps(
                positive_contract,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    oracle["positive_outcome_contracts"] = [positive_contract]
    _attach_exact_origin_boundary(
        research=research,
        verification=verification,
        oracle=oracle,
        positive_contract=positive_contract,
        mechanism_evidence_ids=[str(mechanism_evidence["mechanism_evidence_id"])],
        atom_id="atom:test",
    )
    verification["outcome_oracles"] = [oracle]
    selector_identity = {
        "kind": "evidence_selector",
        "entrypoint": "tests/test_core.py::test_missing_guard",
    }
    failure_path = {
        "verification_method": "runner_controlled_failure_path_v1",
        "path_name": selector_identity["entrypoint"],
        "consumer_identity": selector_identity,
        "independence_key": sha256(
            json.dumps(
                selector_identity,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "hypothesis_id": "h1",
        "support_experiment_id": "exp-1",
        "support_selection_id": "h1:exp-1",
        "control_verification_id": control_receipt["control_verification_id"],
        "mechanism_symbols": ["core.run"],
        "origin_atom_ids": ["atom:test"],
        "observed_failure": {
            "source": "exit_code",
            "difference_kind": "failing_exit_to_zero",
            **control_receipt["observable_difference"]["support"],
        },
    }
    failure_path["failure_path_id"] = (
        "failure_path:"
        + sha256(
            json.dumps(
                failure_path,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    verification["failure_paths"] = [failure_path]
    verification["receipt_sha256"] = evidence_verification_sha256(verification)
    research["evidence_verification"] = verification
    option["scope_evidence"] = {
        "scope_level": "single_path",
        "independent_consumers_or_failure_paths": [
            {
                "name": failure_path["path_name"],
                "evidence_refs": [failure_path["failure_path_id"]],
            }
        ],
    }
    selection["falsification_review"]["evidence_refs"] = [
        {
            "ref": mechanism_evidence["mechanism_evidence_id"],
            "finding": "The verified control bounds the local guard mechanism.",
        }
    ]
    selection["falsification_review"]["outcome_strategy_review"]["evidence_refs"] = [
        mechanism_evidence["mechanism_evidence_id"]
    ]
    selection["falsification_review"]["selected_positive_outcome_contract_id"] = positive_contract[
        "positive_outcome_contract_id"
    ]
    selection["falsification_review"]["outcome_contract_reviews"] = [
        {
            "positive_outcome_contract_id": positive_contract["positive_outcome_contract_id"],
            "verdict": "sufficient",
            "semantic_relation_assessment": (
                "The expected output proves the reproduced guard path completed."
            ),
            "proves_intended_operation": True,
            "problem_coverage": "full",
            "residual_untested_paths": [],
            "evidence_refs": [mechanism_evidence["mechanism_evidence_id"]],
        }
    ]
    selection["falsification_review"] = bind_falsification_review(
        selection["falsification_review"],
        problem_id=pid,
        selected_option=option,
        research=research,
    )
    plan = {
        "change_plan_id": f"plan:{ticket.get('ticket_id', 'test')}:1",
        "case_id": case_id,
        "problem_id": pid,
        "selected_option_id": option_id,
        "title": str(ticket.get("title") or "Plan"),
        "problem": str(ticket.get("problem") or "Problem"),
        "user_impact": "The original workflow cannot complete",
        "proposed_fix": "Apply the traced guard",
        "implementation_steps": ["Update `src/core.py` at `run` to apply the guard."],
        "verification_steps": ["Run the focused regression."],
        "success_criteria": ["The original scenario passes."],
        "rollback_notes": "Revert the guard.",
        "suggested_owner": str(ticket.get("suggested_owner") or "core"),
        "repo_revision": "abc123",
        "change_targets": [
            {
                "action": "modify",
                "path": "src/core.py",
                "symbols": ["core.run"],
                "change": "Apply the guard at the verified local decision.",
            }
        ],
        "verification_commands": ["python -m pytest -q tests/test_core.py::test_missing_guard"],
        "outcome_verification_roles": {
            "original_scenario": {
                "description": "Replay the exact post-change original scenario.",
                "research_experiment_id": "exp-1",
                "commands": ["python -m pytest -q tests/test_core.py::test_missing_guard"],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0},
                    {
                        "type": "command_stdout_contains",
                        "command_index": 0,
                        "value": "guard applied",
                    },
                ],
            },
            "live": None,
            "mitigation_effect": None,
            "recurrence": {
                "description": "Probe the canonical case for fresh recurrence evidence.",
                "verification_owner": "centralized_case_refresh",
                "commands": [],
                "predicates": [],
            },
        },
        "before_after_reproduction": {
            "original_scenario": "Execute the focused case",
            "research_experiment_id": "exp-1",
            "before_change": {
                "command": ("python -m pytest -q tests/test_core.py::test_missing_guard"),
                "expected_exit_code": 1,
                "expected_result": "fails",
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
            },
            "after_change": {
                "command": ("python -m pytest -q tests/test_core.py::test_missing_guard"),
                "expected_exit_code": 0,
                "expected_result": "passes",
                "observable_assertions": [
                    {
                        "source": "exit_code",
                        "operator": "equals",
                        "expected": 0,
                    },
                    {
                        "source": "stdout",
                        "operator": "contains",
                        "expected": "guard applied",
                    },
                ],
            },
            "expected_outcome_state": "resolved",
            "proof_limitation": None,
            "alternate_verification": None,
        },
        "compatibility_and_failure_modes": {
            "preserved_behaviors": ["Existing successful path remains unchanged"],
            "intentional_changes": [],
            "failure_modes": ["Invalid input still fails closed"],
            "migration_required": False,
        },
        "causal_coverage": coverage,
        "scope_evidence": option["scope_evidence"],
        "requires_live_verification": False,
        "live_verification_rationale": "Static-only source defect has no runtime provenance.",
    }
    target_contract_payload = {
        "schema_version": 2,
        "contract_source": "runner_stage6_target_intent_v2",
        "case_id": case_id,
        "problem_id": pid,
        "selected_option_id": option_id,
        "repo_revision": "abc123",
        "targets": [
            {
                "action": "modify",
                "path": "src/core.py",
                "symbols": ["core.run"],
                "change": "Apply the guard at the verified local decision.",
                "change_sha256": sha256(
                    b"Apply the guard at the verified local decision."
                ).hexdigest(),
                "target_role": "production",
            }
        ],
    }
    plan["target_contract"] = {
        **target_contract_payload,
        "contract_sha256": sha256(
            json.dumps(
                target_contract_payload,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    }
    ticket.update(
        {
            "problem_record": {
                "case_id": case_id,
                "canonical_problem_id": pid,
                "case_member_problem_ids": [pid],
                "problem_id": pid,
                "title": ticket.get("title"),
                "problem": ticket.get("problem"),
                "user_impact": "The original workflow cannot complete",
                "evidence_summary": "exp-1",
            },
            "research": research,
            "priority": {
                "case_id": case_id,
                "problem_id": pid,
                "selected_for_research": True,
                "priority_bucket": "p1",
                "priority_rationale": "The mechanism has user impact.",
                "priority_status": "prioritized",
            },
            "solution_options": [option],
            "selected_solution": selection,
            "change_plan": assign_plan_revision_id(
                bind_plan_outcome_oracle(plan, research=research, selection=selection)
            ),
        }
    )
    return ticket


def test_export_body_embeds_exact_hashed_stage6_verification_contract() -> None:
    ticket = _with_strict_readiness(
        {
            "ticket_id": "BLG-PROVENANCE",
            "title": "Bind verification to the plan",
            "problem": "Generic passing checks can overclaim plan verification.",
            "severity": "high",
            "stage": "ready_for_ticket",
            "change_surface": {
                "user_visible": False,
                "kinds": ["behavior_change"],
                "notes": "Internal contract",
            },
            "suggested_owner": "core",
        }
    )
    fingerprint = ticket_export_fingerprint(ticket)
    body = export_commands._render_export_issue_body(
        ticket=ticket,
        fingerprint=fingerprint,
        export_kind="implementation",
        surface_area_high=set(),
    )

    plan = ticket["change_plan"]
    assert isinstance(plan, dict)
    contract = parse_verification_contract_markdown(body)
    assert contract is not None
    assert contract["commands"] == plan["verification_commands"]
    target_contract = parse_plan_target_contract_markdown(body)
    assert target_contract == plan["target_contract"]
    assert f"- Case ID: `{plan['case_id']}`" in body
    assert f"- Plan revision ID: `{plan['plan_revision_id']}`" in body
    for heading in (
        "### Full verified research proof",
        "### Selected causal coverage",
        "### Selected scope evidence",
        "### Adversarial falsification review",
        "### Exact change targets",
        "### Machine-verifiable implementation scope contract",
        "### Original-scenario before / after proof",
        "### Compatibility and failure modes",
        "### Plan causal coverage",
        "### Plan scope evidence",
        "### Outcome verification requirement",
    ):
        assert heading in body
    assert '"target_symbol": "core.run"' in body
    assert '"research_experiment_id": "exp-1"' in body
    assert "Requires live verification: `false`" in body


def _runner_receipt(
    *, case_id: str, plan_revision_id: str, evidence_kind: str
) -> dict[str, object]:
    common = {
        "producer": "usertest_implement",
        "verification_producer": "runner_core",
        "evidence_kind": evidence_kind,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "fingerprint": "1" * 16,
        "ticket_body_sha256": "4" * 64,
        "local_plan_sha256": "5" * 64,
        "local_plan_filename": "ticket.md",
        "verification_contract_sha256": "6" * 64,
        "target_contract_sha256": "8" * 64,
    }
    if evidence_kind == "test":
        return {
            **common,
            "receipt_schema_version": 2,
            "run_dir": "runs/test",
            "verification_path": "runs/test/verification.json",
            "verification_sha256": "2" * 64,
            "ticket_ref_path": "runs/test/ticket.json",
            "ticket_ref_sha256": "3" * 64,
            "verification_binding_sha256": "7" * 64,
            "commands": ["pytest -q tests/test_core.py"],
        }
    return {
        **common,
        "receipt_schema_version": 3,
        "role_artifact_path": f"runs/{evidence_kind}/outcome_role.json",
        "role_artifact_sha256": "2" * 64,
        "role_contract_sha256": "7" * 64,
        "merged_commit": "abc123",
        "verified_implementation_head": "abc123",
    }


def _outcome_record(
    *,
    case_id: str,
    plan_revision_id: str,
    state: str,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "case_id": case_id,
        "plan_revision_id": plan_revision_id,
        "state": state,
        "recorded_at": "2026-07-09T00:00:00Z",
        "requires_live_verification": False,
        "target_branch": "dev",
        "merged_commit": "abc123",
        "test_evidence": [
            {
                "kind": "pytest",
                "reference": "tests/test_core.py",
                "result": "passed",
                "runner_receipt": _runner_receipt(
                    case_id=case_id,
                    plan_revision_id=plan_revision_id,
                    evidence_kind="test",
                ),
            }
        ],
        "original_scenario_evidence": [],
        "live_evidence": [],
        "mitigation_evidence": [],
        "remaining_risks": [],
        "recurrence_check": {"status": "not_run"},
    }
    if state == "resolved":
        record["original_scenario_evidence"] = [
            {
                "kind": "replay",
                "reference": "runs/replay/report.json",
                "result": "passed",
                "runner_receipt": _runner_receipt(
                    case_id=case_id,
                    plan_revision_id=plan_revision_id,
                    evidence_kind="original_scenario",
                ),
            }
        ]
        record["recurrence_check"] = {
            "status": "completed",
            "result": "passed",
            "evidence": [
                {
                    "kind": "replay",
                    "reference": "runs/replay/recurrence.json",
                    "result": "passed",
                    "runner_receipt": _runner_receipt(
                        case_id=case_id,
                        plan_revision_id=plan_revision_id,
                        evidence_kind="recurrence",
                    ),
                }
            ],
        }
    return record


def test_forged_structural_resolution_cannot_suppress_export(
    tmp_path: Path,
) -> None:
    outcome = _outcome_record(
        case_id="case:forged",
        plan_revision_id="plan:forged",
        state="resolved",
    )

    suppresses, provenance = export_commands._verified_outcome_suppresses_export(
        outcome,
        trusted_runs_roots=[tmp_path / "runs"],
        owner_roots=[tmp_path / "owner"],
        case_registry={"schema_version": 1, "cases": {}},
    )

    assert suppresses is False
    assert provenance["verified"] is False
    assert provenance["errors"]


def test_reports_export_tickets_applies_stage_gates_and_ledger_skip(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket_research = {
        "ticket_id": "BLG-001",
        "title": "Add a new top-level mode for onboarding",
        "problem": "New users struggle to discover the right entry points.",
        "severity": "medium",
        "confidence": 0.7,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_top_level_mode"],
            "notes": "New mode proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    ticket_gated = {
        "ticket_id": "BLG-002",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "runner_core",
    }
    ticket_impl = {
        "ticket_id": "BLG-003",
        "title": "Add quickstart examples to README",
        "problem": "README lacks a runnable example.",
        "severity": "high",
        "confidence": 0.9,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:suggested_change:1"],
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": "Docs only."},
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    ticket_triage = {
        "ticket_id": "BLG-004",
        "title": "Clarify unsupported `uvx` mention in quickstart docs",
        "problem": (
            "Docs references look inconsistent and need triage before filing implementation."
        ),
        "severity": "low",
        "confidence": 0.55,
        "stage": "triage",
        "evidence_atom_ids": ["target_a/20260103T000000Z/gemini/0:confusion_point:1"],
        "change_surface": {"user_visible": False, "kinds": ["unknown"], "notes": ""},
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket_research, ticket_gated, ticket_impl, ticket_triage],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    fingerprint_research = ticket_export_fingerprint(ticket_research)
    fingerprint_gated = ticket_export_fingerprint(ticket_gated)
    fingerprint_impl = ticket_export_fingerprint(ticket_impl)
    fingerprint_triage = ticket_export_fingerprint(ticket_triage)
    _write_yaml(
        actions_path,
        {
            "version": 1,
            "actions": [
                {
                    "fingerprint": fingerprint_impl,
                    "status": "filed",
                    "issue_url": "https://example.invalid/issues/123",
                    "notes": "Already filed.",
                }
            ],
        },
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": "target_a/20260101T000000Z/codex/0:confusion_point:1",
                    "status": "ticketed",
                    "fingerprints": [fingerprint_research],
                },
                {
                    "atom_id": "target_a/20260102T000000Z/claude/0:report_validation_error:1",
                    "status": "ticketed",
                    "fingerprints": [fingerprint_gated],
                },
                {
                    "atom_id": "target_a/20260103T000000Z/gemini/0:confusion_point:1",
                    "status": "ticketed",
                    "fingerprints": [fingerprint_triage],
                },
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    assert out_json.exists()
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))

    assert export_doc["stats"]["exports_total"] == 3
    assert export_doc["stats"]["skipped_actioned"] == 1
    assert export_doc["stats"]["idea_files_written"] == 3
    assert export_doc["inputs"]["atom_actions_yaml"] == str(atom_actions_path)
    atom_updates = export_doc["stats"]["atom_status_updates"]
    assert atom_updates["queued_atoms_touched"] == 3
    idea_files = export_doc["idea_files"]
    assert isinstance(idea_files, list)
    assert len(idea_files) == 3
    for path_s in idea_files:
        assert Path(path_s).exists()

    exports = export_doc["exports"]
    assert isinstance(exports, list)
    kinds = {item["source_ticket"]["fingerprint"]: item["export_kind"] for item in exports}
    assert kinds[fingerprint_research] == "research"
    assert kinds[fingerprint_gated] == "research"
    assert kinds[fingerprint_triage] == "research"
    triage_export = next(
        item for item in exports if item["source_ticket"]["fingerprint"] == fingerprint_triage
    )
    assert triage_export["source_ticket"]["stage"] == "triage"
    assert triage_export["title"].startswith("[Research]")
    by_ticket = {item["source_ticket"]["fingerprint"]: item for item in exports}

    owner_research = by_ticket[fingerprint_research]["owner_repo"]
    assert isinstance(owner_research, dict)
    assert Path(owner_research["idea_path"]).exists()
    assert str(owner_repo) in owner_research["idea_path"]

    owner_runner = by_ticket[fingerprint_gated]["owner_repo"]
    assert isinstance(owner_runner, dict)
    assert Path(owner_runner["idea_path"]).exists()
    assert str(repo_root) in owner_runner["idea_path"]
    assert owner_runner["resolution"] == "suggested_owner:runner_core"

    owner_triage = by_ticket[fingerprint_triage]["owner_repo"]
    assert isinstance(owner_triage, dict)
    assert Path(owner_triage["idea_path"]).exists()
    assert ".agents/plans/0.5 - to_triage/" in owner_triage["idea_path"].replace("\\", "/")

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = {item["atom_id"]: item for item in atom_actions_doc["atoms"]}
    assert atoms["target_a/20260101T000000Z/codex/0:confusion_point:1"]["status"] == "queued"
    legacy_failure_atom_id = "target_a/20260102T000000Z/claude/0:report_validation_error:1"
    canonical_failure_atom_id = "target_a/20260102T000000Z/claude/0:run_failure_event:1"
    assert atoms[canonical_failure_atom_id]["status"] == "queued"
    assert legacy_failure_atom_id in atoms[canonical_failure_atom_id]["derived_from_atom_ids"]
    assert atoms["target_a/20260103T000000Z/gemini/0:confusion_point:1"]["status"] == "queued"
    assert any(
        str(owner_research["idea_path"]) == path
        for path in atoms["target_a/20260101T000000Z/codex/0:confusion_point:1"]["queue_paths"]
    )

    research_body = next(
        item["body_markdown"]
        for item in exports
        if item["source_ticket"]["fingerprint"] == fingerprint_research
    )
    assert "Research / ADR Template" in research_body


def test_reports_export_tickets_skips_discarded_fingerprints_by_default(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Discarded generated solution should not re-export",
        "problem": "The generated solution was rejected.",
        "severity": "high",
        "confidence": 0.9,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:suggested_change:1"],
        "change_surface": {"user_visible": True, "kinds": ["docs_change"], "notes": ""},
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    fingerprint = ticket_export_fingerprint(ticket)
    _write_json(
        compiled_dir / "target_a.backlog.json",
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(
        actions_path,
        {
            "version": 1,
            "actions": [
                {
                    "fingerprint": fingerprint,
                    "status": "discarded",
                    "discard_reason": "bad_solution",
                }
            ],
        },
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )

    assert exc.value.code == 0
    export_doc = json.loads((compiled_dir / "target_a.tickets_export.json").read_text())
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_discarded"] == 1
    assert export_doc["stats"]["skipped_actioned"] == 0
    assert export_doc["filters"]["include_discarded"] is False


def test_reports_export_tickets_include_full_stage_context_in_ticket_body(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-CTX-001",
        "title": "Harden batch resume flow after preflight failures",
        "problem": (
            "Batch runs can stall because the operator lacks enough context to recover "
            "the failed ticket correctly."
        ),
        "user_impact": (
            "Operators spend time rediscovering prior research and may reopen the "
            "wrong recovery path."
        ),
        "severity": "high",
        "confidence": 0.82,
        "stage": "ready_for_ticket",
        "evidence_summary": "Multiple runs show the same preflight-failure recovery confusion.",
        "evidence_atom_ids": ["target_a/20260104T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": False,
            "kinds": ["behavior_change"],
            "notes": "Maintenance flow.",
        },
        "breadth": {
            "missions": 2,
            "targets": 1,
            "repo_inputs": 1,
            "agents": 2,
            "personas": 1,
            "runs": 5,
        },
        "suggested_owner": "usertest_implement",
        "problem_record": {
            "problem_id": "problem:batch_resume",
            "evidence_summary": (
                "Operators repeatedly ask what to do after a preflight failure in batch mode."
            ),
        },
        "research": {
            "problem_id": "problem:batch_resume",
            "reproduction_status": "reproduced",
            "writes_used": False,
            "writes_purpose": [],
            "implementation_performed": False,
            "root_cause_hypotheses": [
                "The exported ticket strips away the stage-3/4/5 context needed "
                "for confident implementation.",
            ],
            "broader_class_assessment": "repeated_variant",
            "unknowns": [
                "Whether the current resume path should branch differently for "
                "preflight vs runtime failures."
            ],
            "diff_classification": "no_changes",
            "diff_suspicious_reasons": ["No code changes were needed to reproduce the confusion."],
        },
        "solution_options": [
            {
                "option_id": "opt:compact",
                "problem_id": "problem:batch_resume",
                "family_id": "minimal_patch",
                "summary": "Add one more hint to the current ticket.",
                "tradeoffs": "Lowest lift but still leaves prior reasoning fragmented.",
                "recurrence_prevention": "Partial.",
                "change_surface_hypothesis": "behavior_change",
                "test_implications": "Small ticket rendering assertions.",
                "rationale": "Useful only as a fallback.",
            },
            {
                "option_id": "opt:full_context",
                "problem_id": "problem:batch_resume",
                "family_id": "existing_surface_hardening",
                "summary": (
                    "Render the full research, solutioning, and plan context "
                    "directly into the exported ticket."
                ),
                "tradeoffs": (
                    "Longer ticket body, but implementers no longer need to "
                    "reconstruct the decision trail."
                ),
                "recurrence_prevention": "Makes future implementations self-contained.",
                "change_surface_hypothesis": "behavior_change",
                "test_implications": (
                    "Assert exported tickets include research/selection/plan sections."
                ),
                "rationale": "Best matches the maintenance goal of reducing rediscovery.",
            },
        ],
        "selected_solution": {
            "problem_id": "problem:batch_resume",
            "selected_option_id": "opt:full_context",
            "selected_family_id": "existing_surface_hardening",
            "selection_rationale": (
                "Implementers should not need to reopen multiple stage artifacts "
                "to understand the intended change."
            ),
            "repo_intent_alignment": (
                "Improves operator throughput without expanding external surface area."
            ),
            "why_other_options_were_not_selected": (
                "The compact option still hid key research and tradeoff context."
            ),
            "needs_ux_review": False,
            "component": "usertest_implement",
            "intent_risk": "low",
            "selected_option": {
                "summary": (
                    "Render the full research, solutioning, and plan context "
                    "directly into the exported ticket."
                ),
                "tradeoffs": "Longer markdown, but less rediscovery.",
                "recurrence_prevention": "The ticket becomes a complete handoff artifact.",
                "change_surface_hypothesis": "behavior_change",
                "test_implications": ("Update export ticket tests to verify the new sections."),
                "rationale": "Preserves the value of earlier pipeline stages.",
            },
        },
        "change_plan": {
            "change_plan_id": "plan:batch_resume:1",
            "problem_id": "problem:batch_resume",
            "selected_option_id": "opt:full_context",
            "title": "Expand exported implementation tickets with full planning context",
            "problem": "The exported implementation ticket omits prior reasoning.",
            "user_impact": "Implementers waste time rediscovering context.",
            "proposed_fix": (
                "Include research, selected-solution rationale, and option "
                "tradeoffs in the exported ticket body."
            ),
            "implementation_steps": [
                "Add stage-context sections to the export renderer.",
                "Cover the new sections with export ticket tests.",
            ],
            "verification_steps": [
                "Run the export ticket test module.",
                "Spot-check a generated ready ticket for readability.",
            ],
            "success_criteria": [
                "Ready tickets contain the research context, chosen approach "
                "rationale, and implementation plan.",
            ],
            "rollback_notes": (
                "Revert the renderer changes if the exported ticket becomes too noisy."
            ),
            "suggested_owner": "usertest_implement",
            "related_change_plan_ids": ["plan:batch_resume:2"],
            "change_plan_status": "planned",
        },
        "implementation_steps": [
            "Add stage-context sections to the export renderer.",
            "Cover the new sections with export ticket tests.",
        ],
        "verification_steps": [
            "Run the export ticket test module.",
            "Spot-check a generated ready ticket for readability.",
        ],
        "success_criteria": [
            "Ready tickets contain the research context, chosen approach "
            "rationale, and implementation plan.",
        ],
        "rollback_notes": ("Revert the renderer changes if the exported ticket becomes too noisy."),
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    body = export_doc["exports"][0]["body_markdown"]
    assert "## Research context" in body
    assert "### Root cause hypotheses" in body
    assert "## Selected solution context" in body
    assert "### Why other options were not selected" in body
    assert "## Solution options considered" in body
    assert "### `opt:full_context` (selected)" in body
    assert "## Implementation plan" in body
    assert "### Verification steps" in body


def test_resolve_owner_repo_root_normalizes_local_and_remote_repo_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from usertest_backlog import shared as backlog_shared

    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        backlog_shared,
        "_git_remote_urls",
        lambda _repo_root: ["https://github.com/jcmullwh/usertest.git"],
    )

    owner_root, owner_input, resolution = backlog_shared._resolve_owner_repo_root(
        ticket={
            "repo_inputs_citing": [
                str(repo_root),
                "https://github.com/jcmullwh/usertest.git",
            ]
        },
        scope_repo_input=None,
        cli_repo_input=None,
        repo_root=repo_root,
    )

    assert owner_root == repo_root
    assert owner_input == str(repo_root)
    assert resolution == "ticket_repo_inputs_citing_normalized"


def test_reports_export_tickets_skips_when_plan_ticket_fingerprint_exists(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "docs",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "input": {"breadth_profile": "internal_maintenance"},
            "tickets": [ticket],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    fingerprint = ticket_export_fingerprint(ticket)
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)
    (complete_dir / f"20260211_BLG-999_{fingerprint}_already-done.md").write_text(
        "# Already done\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["idea_files_written"] == 0

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = {item["atom_id"]: item for item in atom_actions_doc["atoms"]}
    legacy_failure_atom_id = "target_a/20260102T000000Z/claude/0:report_validation_error:1"
    canonical_failure_atom_id = "target_a/20260102T000000Z/claude/0:run_failure_event:1"
    assert atoms[canonical_failure_atom_id]["status"] == "actioned"
    assert legacy_failure_atom_id in atoms[canonical_failure_atom_id]["derived_from_atom_ids"]


def test_export_quarantines_only_ticket_matching_nul_archived_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    affected_atom_id = "target_a/20260102T000000Z/codex/0:suggested_change:1"
    unrelated_atom_id = "target_a/20260102T000000Z/codex/0:suggested_change:2"
    affected = _with_strict_readiness(
        {
            "ticket_id": "BLG-001",
            "title": "Repair corrupt historical plan handling",
            "problem": "A historical plan copy contains untrusted bytes.",
            "severity": "high",
            "confidence": 0.9,
            "stage": "ready_for_ticket",
            "evidence_atom_ids": [affected_atom_id],
            "change_surface": {"user_visible": False, "kinds": []},
            "suggested_owner": "backlog_repo",
        }
    )
    unrelated = _with_strict_readiness(
        {
            "ticket_id": "BLG-002",
            "title": "Export unrelated valid plan",
            "problem": "An independent evidenced change is ready to export.",
            "severity": "high",
            "confidence": 0.9,
            "stage": "ready_for_ticket",
            "evidence_atom_ids": [unrelated_atom_id],
            "change_surface": {"user_visible": False, "kinds": []},
            "suggested_owner": "backlog_repo",
        }
    )
    monkeypatch.setattr(
        export_commands,
        "verify_persisted_research_evidence",
        lambda _dossier: (True, []),
    )
    affected_fingerprint = ticket_export_fingerprint(affected)
    unrelated_fingerprint = ticket_export_fingerprint(unrelated)
    assert affected_fingerprint != unrelated_fingerprint

    _write_json(
        compiled_dir / "target_a.backlog.json",
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [affected, unrelated],
        },
    )
    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {"atom_id": affected_atom_id, "status": "new"},
                {"atom_id": unrelated_atom_id, "status": "new"},
            ],
        },
    )

    archived_dir = owner_repo / ".agents" / "plans" / "6 - archived"
    archived_dir.mkdir(parents=True, exist_ok=True)
    corrupt_path = archived_dir / f"20260709_{affected_fingerprint}_corrupt.md"
    original_bytes = b'# unrelated batch payload\n\x00{"not": "a plan"}\n'
    corrupt_path.write_bytes(original_bytes)

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["exports_total"] == 1
    assert export_doc["stats"]["skipped_integrity_unknown"] == 1
    assert export_doc["stats"]["skipped_existing_plan"] == 0
    assert export_doc["exports"][0]["fingerprint"] == unrelated_fingerprint
    assert export_doc["integrity_unknown_skips"] == [
        {
            "fingerprint": affected_fingerprint,
            "owner_root": str(owner_repo),
            "reasons": ["plan_copy_contains_nul_byte"],
            "paths": [str(corrupt_path)],
        }
    ]
    assert corrupt_path.read_bytes() == original_bytes
    assert not list(
        (owner_repo / ".agents" / "plans" / "2 - ready").glob(f"*{affected_fingerprint}*.md")
    )
    unrelated_idea_path = Path(export_doc["exports"][0]["owner_repo"]["idea_path"])
    assert unrelated_idea_path.exists()
    assert unrelated_fingerprint in unrelated_idea_path.name

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = {item["atom_id"]: item for item in atom_actions_doc["atoms"]}
    assert atoms[affected_atom_id]["status"] == "new"
    assert atoms[unrelated_atom_id]["status"] == "queued"


def test_reports_export_tickets_preserves_duplicates_for_explicit_maintenance(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "docs",
    }

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    fingerprint = ticket_export_fingerprint(ticket)
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)
    complete_path = complete_dir / f"20260211_{fingerprint}_already-done.md"
    complete_path.write_text("# Already done\n", encoding="utf-8")

    ideas_dir = owner_repo / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    stale_idea_path = ideas_dir / f"20260212_{fingerprint}_stale-queue-copy.md"
    stale_idea_path.write_text("# Stale copy\n", encoding="utf-8")

    assert complete_path.exists()
    assert stale_idea_path.exists()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["idea_files_written"] == 0

    assert complete_path.exists()
    assert stale_idea_path.exists()


@pytest.mark.parametrize(
    "bucket",
    [
        "2 - ready",
        "3 - in_progress",
        "4 - for_review",
        "6 - archived",
        "0.1 - deferred",
    ],
)
def test_reports_export_tickets_dedupes_active_and_deferred_plan_buckets(
    tmp_path: Path,
    bucket: str,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Keep existing plan dedupe active",
        "problem": "Duplicate plan files should not be generated.",
        "severity": "high",
        "confidence": 0.8,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:suggested_change:1"],
        "change_surface": {"user_visible": False, "kinds": []},
        "suggested_owner": "docs",
    }
    fingerprint = ticket_export_fingerprint(ticket)

    _write_json(
        compiled_dir / "target_a.backlog.json",
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    existing_dir = owner_repo / ".agents" / "plans" / bucket
    existing_dir.mkdir(parents=True, exist_ok=True)
    existing_path = existing_dir / f"20260211_{fingerprint}_existing.md"
    existing_path.write_text("# Existing plan\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads((compiled_dir / "target_a.tickets_export.json").read_text())
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["idea_files_written"] == 0
    assert existing_path.exists()


def test_reports_export_tickets_ignores_stray_discarded_plan_for_dedupe(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Regenerate after rejected generated solution",
        "problem": "A discarded markdown file without a ledger entry should not block export.",
        "severity": "high",
        "confidence": 0.8,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:suggested_change:1"],
        "change_surface": {"user_visible": False, "kinds": []},
        "suggested_owner": "docs",
    }
    _with_strict_readiness(ticket)
    fingerprint = ticket_export_fingerprint(ticket)

    _write_json(
        compiled_dir / "target_a.backlog.json",
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    discarded_dir = owner_repo / ".agents" / "plans" / "0.2 - discarded"
    discarded_dir.mkdir(parents=True, exist_ok=True)
    discarded_path = discarded_dir / f"20260211_{fingerprint}_bad-solution.md"
    discarded_path.write_text("# Rejected generated plan\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads((compiled_dir / "target_a.tickets_export.json").read_text())
    assert export_doc["stats"]["exports_total"] == 1
    assert export_doc["stats"]["skipped_existing_plan"] == 0
    assert export_doc["stats"]["skipped_discarded"] == 0
    assert export_doc["stats"]["idea_files_written"] == 1
    assert discarded_path.exists()
    assert list((owner_repo / ".agents" / "plans" / "2 - ready").glob(f"*{fingerprint}*.md"))


def test_cleanup_stale_ticket_idea_files_includes_owner_repo_root_when_no_repo_inputs(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo_root"
    repo_root.mkdir(parents=True, exist_ok=True)
    owner_repo_root = tmp_path / "owner_repo_root"
    owner_repo_root.mkdir(parents=True, exist_ok=True)

    fingerprint = "deadbeefdeadbeef"
    ideas_dir = owner_repo_root / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    stale_idea_path = ideas_dir / f"20260212_{fingerprint}_stale-queue-copy.md"
    stale_idea_path.write_text(
        "# Stale copy\n\n"
        "Generated by `python -m usertest_backlog.cli reports export-tickets` on "
        "2026-07-09T00:00:00Z.\n"
        f"- Fingerprint: `{fingerprint}`\n"
        "- Export scope target: `target_a`\n",
        encoding="utf-8",
    )
    assert stale_idea_path.exists()

    _cleanup_stale_ticket_idea_files(
        ticket={},
        fingerprint=fingerprint,
        owner_repo_root=owner_repo_root,
        repo_root=repo_root,
        scope_repo_input=None,
        cli_repo_input=None,
    )

    assert not stale_idea_path.exists()
    archived = owner_repo_root / ".agents" / "plans" / "6 - archived" / stale_idea_path.name
    assert archived.exists()
    assert '"state": "duplicate"' in archived.read_text(encoding="utf-8")


def test_cleanup_and_refresh_preserve_human_authored_matching_file(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    queue_dir = owner_root / ".agents" / "plans" / "1 - ideas"
    queue_dir.mkdir(parents=True)
    fingerprint = "deadbeefdeadbeef"
    human_path = queue_dir / f"20260709_{fingerprint}_human-note.md"
    original = "# Human-authored note\n\nDo not overwrite or archive this file.\n"
    human_path.write_text(original, encoding="utf-8")

    assert _extract_generated_ticket_scope_metadata(human_path) is None
    refreshed = _refresh_generated_ticket_idea_file(
        path=human_path,
        issue_title="Generated replacement",
        fingerprint=fingerprint,
        body_markdown="generated",
        scope_target="target_a",
        scope_repo_input=str(owner_root),
        execution_domain=None,
        execution_conflict_keys=[],
        ux_review_section=None,
    )
    assert refreshed is False

    _cleanup_stale_ticket_idea_files(
        ticket={},
        fingerprint=fingerprint,
        owner_repo_root=owner_root,
        repo_root=owner_root,
        scope_repo_input=None,
        cli_repo_input=None,
    )
    assert human_path.read_text(encoding="utf-8") == original


def test_stale_scope_cleanup_archives_only_with_canonical_case_replacement(tmp_path: Path) -> None:
    owner_root = tmp_path / "owner"
    queue_dir = owner_root / ".agents" / "plans" / "2 - ready"
    queue_dir.mkdir(parents=True)
    old_fingerprint = "1111111111111111"
    new_fingerprint = "2222222222222222"
    old_path = queue_dir / f"20260709_{old_fingerprint}_old-plan.md"
    old_path.write_text(
        "# Old plan\n\n"
        "Generated by `python -m usertest_backlog.cli reports export-tickets` on "
        "2026-07-09T00:00:00Z.\n"
        f"- Fingerprint: `{old_fingerprint}`\n"
        "- Case ID: `case:shared`\n"
        "- Plan revision ID: `planrev:case:shared:old:1`\n"
        "- Export scope target: `target_a`\n",
        encoding="utf-8",
    )

    result = _cleanup_stale_generated_scope_ticket_files(
        owner_repo_root=owner_root,
        target_slug="target_a",
        keep_fingerprints={new_fingerprint},
        keep_fingerprint_by_case_id={"case:shared": new_fingerprint},
    )

    assert result == {"archived": 1, "unresolved": 0}
    assert not old_path.exists()
    archived = owner_root / ".agents" / "plans" / "6 - archived" / old_path.name
    archived_text = archived.read_text(encoding="utf-8")
    assert '"state": "superseded"' in archived_text
    assert f'"related_fingerprint": "{new_fingerprint}"' in archived_text


def test_reports_export_tickets_preserves_unprojected_queue_duplicates(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [
                {
                    "ticket_id": "BLG-001",
                    "title": "Something else",
                    "problem": "Irrelevant for sweep.",
                    "severity": "low",
                    "confidence": 0.5,
                    "stage": "ready_for_ticket",
                    "evidence_atom_ids": [],
                    "change_surface": {"user_visible": False, "kinds": [], "notes": ""},
                    "breadth": {
                        "missions": 1,
                        "targets": 1,
                        "repo_inputs": 1,
                        "agents": 1,
                        "runs": 1,
                    },
                    "suggested_owner": "docs",
                }
            ],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    # Fingerprints outside the validated projection require explicit maintenance.
    stale_fp = "deadbeefdeadbeef"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    complete_dir.mkdir(parents=True, exist_ok=True)
    (complete_dir / f"20260211_BLG-999_{stale_fp}_already-done.md").write_text(
        "# Already done\n",
        encoding="utf-8",
    )
    ideas_dir = owner_repo / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    stale_idea_path = ideas_dir / f"20260212_BLG-999_{stale_fp}_stale-queue-copy.md"
    stale_idea_path.write_text("# Stale copy\n", encoding="utf-8")
    assert stale_idea_path.exists()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["swept_actioned_queue_dupes_removed"] == 0
    assert export_doc["stats"]["swept_actioned_queue_dupes_archived"] == 0
    assert stale_idea_path.exists()


def test_reports_export_tickets_preserves_unprojected_bucket_duplicates(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [
                {
                    "ticket_id": "BLG-001",
                    "title": "Sweep trigger",
                    "problem": "Irrelevant for sweep.",
                    "severity": "low",
                    "confidence": 0.5,
                    "stage": "ready_for_ticket",
                    "evidence_atom_ids": [],
                    "change_surface": {"user_visible": False, "kinds": [], "notes": ""},
                    "breadth": {
                        "missions": 1,
                        "targets": 1,
                        "repo_inputs": 1,
                        "agents": 1,
                        "runs": 1,
                    },
                    "suggested_owner": "docs",
                }
            ],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    stale_fp = "deadbeefdeadbeef"
    in_progress_dir = owner_repo / ".agents" / "plans" / "3 - in_progress"
    complete_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    in_progress_dir.mkdir(parents=True, exist_ok=True)
    complete_dir.mkdir(parents=True, exist_ok=True)

    in_progress_path = in_progress_dir / f"20260212_{stale_fp}_stale-in-progress.md"
    complete_path = complete_dir / f"20260212_{stale_fp}_done.md"
    in_progress_path.write_text("# In progress\n", encoding="utf-8")
    complete_path.write_text("# Done\n", encoding="utf-8")
    assert in_progress_path.exists()
    assert complete_path.exists()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["swept_actioned_bucket_dupes_removed"] == 0
    assert export_doc["stats"]["swept_actioned_bucket_dupes_archived"] == 0
    assert in_progress_path.exists()
    assert complete_path.exists()


def test_reports_export_tickets_ux_cannot_promote_shallow_research_ticket(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "medium",
        "confidence": 0.6,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    fingerprint = ticket_export_fingerprint(ticket)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "prompt_hash": "deadbeefdeadbeef",
            "review": {
                "command_surface_budget": {
                    "max_new_commands_per_quarter": 0,
                    "notes": "Keep it tight.",
                },
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "fingerprints": [fingerprint],
                        "recommended_approach": "docs",
                        "proposed_change_surface": {
                            "user_visible": True,
                            "kinds": ["docs_change"],
                            "notes": "Document existing commands instead of adding a new one.",
                        },
                        "rationale": "A new command isn't necessary; docs can remove friction.",
                        "next_steps": ["Update README quickstart with a clear entrypoint."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "notes": "",
                "confidence": 0.8,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["ux_recommendations_loaded"] == 1
    assert export_doc["stats"]["ux_idea_files_updated"] == 1
    assert export_doc["stats"]["exports_total"] == 1

    export = export_doc["exports"][0]
    assert export["export_kind"] == "research"
    assert export["source_ticket"]["stage"] == "research_required"
    assert "ux:docs" in export["labels"]
    assert "## UX review" in export["body_markdown"]
    assert "Raw recommendation JSON" in export["body_markdown"]

    idea_path = Path(export["owner_repo"]["idea_path"])
    assert idea_path.exists()
    assert idea_path.parent.name == "1 - ideas"
    idea_text = idea_path.read_text(encoding="utf-8")
    assert "## UX review" in idea_text
    assert "- Export kind: `research`" in idea_text
    assert "- Stage: `research_required`" in idea_text


def test_reports_export_tickets_does_not_promote_high_surface_ticket_from_ux_review(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-002",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260102T000000Z/claude/0:report_validation_error:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 3, "targets": 2, "repo_inputs": 2, "agents": 2, "runs": 8},
        "suggested_owner": "runner_core",
    }
    _with_strict_readiness(ticket)
    fingerprint = ticket_export_fingerprint(ticket)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "fingerprints": [fingerprint],
                        "recommended_approach": "docs",
                        "rationale": "Prefer docs over new command.",
                        "next_steps": ["Document the existing command flow."],
                        "evidence_breadth_summary": {
                            "missions": 3,
                            "targets": 2,
                            "repo_inputs": 2,
                            "agents": 2,
                            "runs": 8,
                        },
                    }
                ],
                "confidence": 0.8,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 1
    export = export_doc["exports"][0]
    assert export["export_kind"] == "research"
    assert export["source_ticket"]["stage"] == "ready_for_ticket"
    assert "ux:docs" in export["labels"]

    idea_path = Path(export["owner_repo"]["idea_path"])
    assert idea_path.exists()
    assert idea_path.parent.name == "2 - ready"
    idea_text = idea_path.read_text(encoding="utf-8")
    assert "- Export kind: `research`" in idea_text


def test_reports_export_tickets_does_not_promote_research_ticket_from_ux_review(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-003",
        "title": "Fail earlier on incompatible startup capability combinations",
        "problem": "Runs continue with degraded startup behavior instead of stopping loudly.",
        "severity": "medium",
        "confidence": 0.7,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260103T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["breaking_change"],
            "notes": "Existing startup flow becomes stricter.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 2, "runs": 6},
        "suggested_owner": "runner_core",
    }
    _with_strict_readiness(ticket, needs_ux_review=True)
    fingerprint = ticket_export_fingerprint(ticket)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "fingerprints": [fingerprint],
                        "recommended_approach": "accept_existing_surface",
                        "review_domain": "behavior_compat",
                        "breadth_profile": "internal_maintenance",
                        "decision_basis": {
                            "context_breadth": {"missions": 1, "targets": 1, "repo_inputs": 1},
                            "observation_breadth": {"runs": 6, "agents": 2, "personas": 0},
                            "structurally_constant_dimensions": [
                                "missions",
                                "targets",
                                "repo_inputs",
                            ],
                        },
                        "rationale": (
                            "Observation breadth supports hardening existing startup behavior."
                        ),
                        "next_steps": ["Add migration notes and regression tests."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 2,
                            "runs": 6,
                        },
                    }
                ],
                "confidence": 0.8,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 1
    assert export_doc["inputs"]["breadth_profile"] == "external_generalization"
    assert PurePosixPath(export_doc["inputs"]["policy_config"].replace("\\", "/")).parts[-2:] == (
        "configs",
        "backlog_policy.yaml",
    )
    export = export_doc["exports"][0]
    assert export["export_kind"] == "research"
    assert export["source_ticket"]["stage"] == "research_required"
    assert "ux:accept_existing_surface" in export["labels"]
    body = export["body_markdown"]
    assert "## UX review (authoritative)" in body
    assert "accept_existing_surface" in body


def test_reports_export_tickets_updates_existing_plan_ticket_with_ux_review(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "medium",
        "confidence": 0.6,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    _with_strict_readiness(ticket, needs_ux_review=True)
    fingerprint = ticket_export_fingerprint(ticket)
    ready_dir = owner_repo / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    plan_path = ready_dir / f"20260221_{fingerprint}_existing.md"
    plan_path.write_text(
        "\n".join(
            [
                "# [Research] Existing ticket",
                "",
                f"- Fingerprint: `{fingerprint}`",
                "",
                "- Export kind: `research`",
                "- Stage: `research_required`",
                "",
            ]
        ),
        encoding="utf-8",
    )

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "fingerprints": [fingerprint],
                        "recommended_approach": "docs",
                        "rationale": "A new command isn't necessary.",
                        "next_steps": ["Update docs instead."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "confidence": 0.7,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["ux_plan_tickets_updated"] == 1

    updated = plan_path.read_text(encoding="utf-8")
    assert "- Export kind: `research`" in updated


def test_reports_export_tickets_preserves_stale_scope_file_without_case_identity(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-001",
        "title": "Keep current ticket",
        "problem": "Current backlog item should remain queued.",
        "severity": "medium",
        "confidence": 0.8,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {"user_visible": False, "kinds": ["unknown"], "notes": ""},
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    _with_strict_readiness(ticket)
    fingerprint = ticket_export_fingerprint(ticket)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    ideas_dir = owner_repo / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    ready_dir = owner_repo / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    stale_same_target = ideas_dir / "20260301_deadbeefdeadbeef_old-target-a.md"
    stale_same_target.write_text(
        "\n".join(
            [
                "# Old target_a ticket",
                "",
                (
                    "Generated by `python -m usertest_backlog.cli reports export-tickets` "
                    "on 2026-03-01T00:00:00Z."
                ),
                "- Fingerprint: `deadbeefdeadbeef`",
                "- Export scope target: `target_a`",
                "",
                "## Evidence atom ids",
                "",
                "- `target_a/20260101T000000Z/codex/0:confusion_point:1`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    stale_other_target = ideas_dir / "20260301_feedfacefeedface_old-target-b.md"
    stale_other_target.write_text(
        "\n".join(
            [
                "# Old target_b ticket",
                "",
                (
                    "Generated by `python -m usertest_backlog.cli reports export-tickets` "
                    "on 2026-03-01T00:00:00Z."
                ),
                "- Fingerprint: `feedfacefeedface`",
                "- Export scope target: `target_b`",
                "",
                "## Evidence atom ids",
                "",
                "- `target_b/20260101T000000Z/codex/0:confusion_point:1`",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    assert stale_same_target.exists()
    assert stale_other_target.exists()

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["swept_scope_stale_generated_removed"] == 0
    assert export_doc["stats"]["swept_scope_stale_generated_archived"] == 0
    assert export_doc["stats"]["swept_scope_stale_generated_unresolved"] == 1
    generated_candidates = sorted(ready_dir.glob(f"*_{fingerprint}_*.md"))
    assert len(generated_candidates) == 1
    generated_path = generated_candidates[0]
    updated = generated_path.read_text(encoding="utf-8")
    assert "- Stage: `ready_for_ticket`" in updated
    assert "- Export scope target: `target_a`" in updated


def test_reports_export_tickets_refreshes_existing_generated_queue_ticket(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-010",
        "title": "Refresh generated queue ticket",
        "problem": (
            "Existing generated queue ticket should be rewritten with current breadth context."
        ),
        "severity": "high",
        "confidence": 0.8,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {"user_visible": True, "kinds": ["breaking_change"], "notes": ""},
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 2, "runs": 6},
        "breadth_profile": "internal_maintenance",
        "decision_basis": {
            "context_breadth": {"missions": 1, "targets": 1, "repo_inputs": 1},
            "observation_breadth": {"runs": 6, "agents": 2, "personas": 0},
            "structurally_constant_dimensions": ["missions", "targets", "repo_inputs"],
        },
        "execution_domain": "runner_core",
        "execution_conflict_keys": [
            "execution_domain:runner_core",
            "subsystem:python_runtime",
        ],
        "review_domain": "behavior_compat",
        "suggested_owner": "docs",
    }
    _with_strict_readiness(ticket)
    fingerprint = ticket_export_fingerprint(ticket)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "input": {"breadth_profile": "internal_maintenance"},
            "tickets": [ticket],
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    ideas_dir = owner_repo / ".agents" / "plans" / "1 - ideas"
    ideas_dir.mkdir(parents=True, exist_ok=True)
    ready_dir = owner_repo / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    existing_path = ideas_dir / f"20260301_{fingerprint}_old.md"
    existing_path.write_text(
        "\n".join(
            [
                "# Old generated ticket",
                "",
                (
                    "Generated by `python -m usertest_backlog.cli reports export-tickets` "
                    "on 2026-03-01T00:00:00Z."
                ),
                f"- Fingerprint: `{fingerprint}`",
                "",
                "- Stage: `ready_for_ticket`",
                "",
                "## Problem-local evidence breadth (counts)",
                "",
                "- missions: 1",
                "- targets: 1",
                "- repo_inputs: 1",
                "- agents: 1",
                "- runs: 1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    promoted_candidates = sorted(ready_dir.glob(f"*_{fingerprint}_*.md"))
    assert len(promoted_candidates) == 1
    promoted_path = promoted_candidates[0]
    assert not existing_path.exists()
    updated = promoted_path.read_text(encoding="utf-8")
    assert "## Evidence breadth context" in updated
    assert "- Breadth profile: `internal_maintenance`" in updated
    assert "- Observation breadth: `runs=6, agents=2, personas=0`" in updated
    assert "- Export scope target: `target_a`" in updated
    assert "- Execution domain: `runner_core`" in updated
    assert (
        "- Execution conflict keys: `execution_domain:runner_core`, `subsystem:python_runtime`"
    ) in updated

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["generated_queue_files_refreshed"] == 1


def test_reports_export_tickets_defers_existing_plan_ticket_and_updates_actions(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-009",
        "title": "Add `usertest smoke` shortcut command",
        "problem": "Operators want a single obvious entry point.",
        "severity": "low",
        "confidence": 0.6,
        "stage": "ready_for_ticket",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["new_command"],
            "notes": "New command proposed.",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }
    fingerprint = ticket_export_fingerprint(ticket)
    ready_dir = owner_repo / ".agents" / "plans" / "2 - ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    plan_path = ready_dir / f"20260221_{fingerprint}_existing.md"
    plan_path.write_text(
        "\n".join(
            [
                "# [Research] Existing ticket",
                "",
                f"- Fingerprint: `{fingerprint}`",
                "",
                "- Export kind: `research`",
                "- Stage: `ready_for_ticket`",
                "",
            ]
        ),
        encoding="utf-8",
    )

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "fingerprints": [fingerprint],
                        "recommended_approach": "defer",
                        "rationale": "Defer the new command.",
                        "next_steps": ["No action."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "confidence": 0.7,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["skipped_existing_plan"] == 1
    assert export_doc["stats"]["ux_tickets_deferred"] == 1

    deferred_dir = owner_repo / ".agents" / "plans" / "0.1 - deferred"
    deferred_matches = list(deferred_dir.glob(f"*{fingerprint}*.md"))
    assert deferred_matches
    assert not plan_path.exists()

    actions_doc = yaml.safe_load(actions_path.read_text(encoding="utf-8"))
    actions_by_fp = {item["fingerprint"]: item for item in actions_doc["actions"]}
    assert actions_by_fp[fingerprint]["status"] == "deferred"


def test_reports_export_tickets_defer_moves_bucket_and_skips_export(tmp_path: Path) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = {
        "ticket_id": "BLG-008",
        "title": "Batch validation UX: aggregate and print all validation errors",
        "problem": "Output is unclear.",
        "severity": "high",
        "confidence": 0.8,
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
        "change_surface": {
            "user_visible": True,
            "kinds": ["behavior_change"],
            "notes": "",
        },
        "breadth": {"missions": 1, "targets": 1, "repo_inputs": 1, "agents": 1, "runs": 1},
        "suggested_owner": "docs",
    }

    fingerprint = ticket_export_fingerprint(ticket)

    backlog_path = compiled_dir / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    _write_json(
        compiled_dir / "target_a.ux_review.json",
        {
            "schema_version": 1,
            "generated_at": "2026-02-21T00:00:00Z",
            "scope": {"target": "target_a", "repo_input": None},
            "status": "ok",
            "review": {
                "recommendations": [
                    {
                        "recommendation_id": "UX-001",
                        "fingerprints": [fingerprint],
                        "recommended_approach": "defer",
                        "rationale": "Already implemented; defer.",
                        "next_steps": ["Re-triage as already implemented."],
                        "evidence_breadth_summary": {
                            "missions": 1,
                            "targets": 1,
                            "repo_inputs": 1,
                            "agents": 1,
                            "runs": 1,
                        },
                    }
                ],
                "confidence": 0.7,
            },
        },
    )

    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(actions_path, {"version": 1, "actions": []})

    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(
        atom_actions_path,
        {
            "version": 1,
            "atoms": [
                {
                    "atom_id": "target_a/20260101T000000Z/codex/0:confusion_point:1",
                    "status": "ticketed",
                    "fingerprints": [fingerprint],
                }
            ],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0

    out_json = compiled_dir / "target_a.tickets_export.json"
    export_doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert export_doc["stats"]["ux_tickets_deferred"] == 1
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["idea_files_written"] == 1
    assert export_doc["exports"] == []

    deferred_dir = owner_repo / ".agents" / "plans" / "0.1 - deferred"
    deferred_matches = list(deferred_dir.glob(f"*{fingerprint}*.md"))
    assert deferred_matches

    actions_doc = yaml.safe_load(actions_path.read_text(encoding="utf-8"))
    assert actions_doc["version"] == 1
    actions_by_fp = {item["fingerprint"]: item for item in actions_doc["actions"]}
    assert actions_by_fp[fingerprint]["status"] == "deferred"

    atom_actions_doc = yaml.safe_load(atom_actions_path.read_text(encoding="utf-8"))
    atoms = {item["atom_id"]: item for item in atom_actions_doc["atoms"]}
    assert atoms["target_a/20260101T000000Z/codex/0:confusion_point:1"]["status"] == "actioned"


def test_export_ignores_unverified_terminal_outcome_while_open_plan_awaits_proof(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    runs_dir = tmp_path / "runs" / "usertest"
    compiled_dir = runs_dir / "target_a" / "_compiled"
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir(parents=True, exist_ok=True)

    ticket = _with_strict_readiness(
        {
            "ticket_id": "BLG-CONFLICT",
            "title": "Guard the traced decision",
            "problem": "The local decision omits a required guard.",
            "severity": "high",
            "confidence": 0.9,
            "stage": "ready_for_ticket",
            "evidence_atom_ids": ["target_a/20260101T000000Z/codex/0:confusion_point:1"],
            "change_surface": {
                "user_visible": False,
                "kinds": ["behavior_change"],
                "notes": "Internal behavior",
            },
            "suggested_owner": "core",
        }
    )
    fingerprint = ticket_export_fingerprint(ticket)
    plan = ticket["change_plan"]
    assert isinstance(plan, dict)
    case_id = str(plan["case_id"])
    plan_revision_id = str(plan["plan_revision_id"])

    plan_dir = owner_repo / ".agents" / "plans" / "5 - complete"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / f"20260709_{fingerprint}_open-outcome.md"
    plan_markdown = "\n".join(
        [
            "# Existing plan",
            "",
            f"- Fingerprint: `{fingerprint}`",
            f"- Case ID: `{case_id}`",
            f"- Plan revision ID: `{plan_revision_id}`",
            "",
        ]
    )
    plan_path.write_text(
        upsert_outcome_markdown(
            plan_markdown,
            _outcome_record(
                case_id=case_id,
                plan_revision_id=plan_revision_id,
                state="tests_verified",
            ),
        ),
        encoding="utf-8",
    )

    _write_json(
        compiled_dir / "target_a.backlog.json",
        {
            "schema_version": 1,
            "scope": {"repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )
    actions_path = tmp_path / "backlog_actions.yaml"
    _write_yaml(
        actions_path,
        {
            "version": 1,
            "actions": [
                {
                    "fingerprint": fingerprint,
                    "status": "actioned",
                    "outcome": _outcome_record(
                        case_id=case_id,
                        plan_revision_id=plan_revision_id,
                        state="resolved",
                    ),
                }
            ],
        },
    )
    atom_actions_path = tmp_path / "backlog_atom_actions.yaml"
    _write_yaml(atom_actions_path, {"version": 1, "atoms": []})

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(runs_dir),
                "--target",
                "target_a",
                "--actions-yaml",
                str(actions_path),
                "--atom-actions-yaml",
                str(atom_actions_path),
            ]
        )
    assert exc.value.code == 0
    export_doc = json.loads(
        (compiled_dir / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["skipped_actioned"] == 0
    assert export_doc["stats"]["skipped_awaiting_outcome_verification"] == 1


def test_unready_filtered_replacement_does_not_supersede_existing_plan(
    tmp_path: Path,
) -> None:
    repo_root = _make_repo_root(tmp_path)
    owner_repo = tmp_path / "owner_repo"
    owner_repo.mkdir()
    ticket: dict[str, object] = {
        "ticket_id": "BLG-UNREADY-REPLACEMENT",
        "title": "Replacement still needs research",
        "problem": "The proposed replacement is not implementation-ready.",
        "severity": "high",
        "stage": "research_required",
        "evidence_atom_ids": ["target_a/run/agent/0:failure:1"],
        "change_surface": {"user_visible": False, "kinds": ["behavior_change"]},
        "suggested_owner": "docs",
    }
    _with_strict_readiness(ticket)
    case_id = ticket_export_case_id(ticket)
    assert case_id is not None
    new_fingerprint = ticket_export_fingerprint(ticket)
    old_fingerprint = "deadbeefdeadbeef"
    old_plan = (
        owner_repo / ".agents" / "plans" / "1 - ideas" / f"20260709_{old_fingerprint}_old-plan.md"
    )
    old_plan.parent.mkdir(parents=True)
    old_plan.write_text(
        "\n".join(
            [
                "# Existing canonical plan",
                "",
                (
                    "Generated by `python -m usertest_backlog.cli reports export-tickets` "
                    "on 2026-07-09T00:00:00Z."
                ),
                f"- Fingerprint: `{old_fingerprint}`",
                f"- Case ID: `{case_id}`",
                "- Export scope target: `target_a`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    backlog_path = tmp_path / "runs" / "target_a" / "_compiled" / "target_a.backlog.json"
    _write_json(
        backlog_path,
        {
            "schema_version": 1,
            "scope": {"target": "target_a", "repo_input": str(owner_repo)},
            "tickets": [ticket],
        },
    )

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "reports",
                "export-tickets",
                "--repo-root",
                str(repo_root),
                "--runs-dir",
                str(tmp_path / "runs"),
                "--target",
                "target_a",
                "--stage",
                "ready_for_ticket",
            ]
        )

    assert exc.value.code == 0
    assert old_plan.exists()
    assert not list(old_plan.parent.glob(f"*{new_fingerprint}*.md"))
    export_doc = json.loads(
        (backlog_path.parent / "target_a.tickets_export.json").read_text(encoding="utf-8")
    )
    assert export_doc["stats"]["exports_total"] == 0
    assert export_doc["stats"]["swept_scope_stale_generated_archived"] == 0
