"""Acceptance coverage for causal evidence surviving the full backlog lifecycle."""

from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
import runner_core.outcome_roles as outcome_roles
import yaml
from agent_adapters.read_attestation import observed_read_attestation
from backlog_core import (
    assess_change_plan_readiness,
    assess_research_readiness,
    assess_selection_readiness,
    assess_solution_option_readiness,
    assess_ticket_readiness,
    assign_plan_revision_id,
    bind_falsification_review,
    bind_plan_outcome_oracle,
    evidence_assignment_sha256,
    infer_live_verification_requirement,
    source_evidence_atom_projection,
)
from backlog_repo import (
    canonical_plan_sha256,
    canonical_ticket_body_sha256,
    extract_outcome_markdown,
    parse_verification_contract_markdown,
    render_verification_contract_markdown,
    upsert_outcome_markdown,
    validate_outcome_record,
)
from backlog_repo.plan_scope import (
    build_plan_target_contract,
    render_plan_target_contract_markdown,
)
from runner_core import RunnerConfig, RunRequest, RunResult, run_outcome_evidence_role
from runner_core.runner import _run_verification_commands
from usertest_implement.outcome_evidence import (
    build_verification_binding,
    validate_bound_runner_verification,
)
import usertest_implement.outcome_progression as outcome_progression
from usertest_implement.outcome_progression import progress_post_merge_outcome
from usertest_implement.selection import _case_plan_fingerprint

import backlog_miner.research_runner as research_runner
from backlog_miner.research_evidence import (
    TrustedHostReplayExecutor,
    verify_persisted_research_evidence,
)

ORIGINAL_COMMAND = "python -m pytest -q -s --tb=native tests/test_core.py::test_reported_failure"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_reproduced_problem(workspace: Path) -> str:
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "core.py").write_text(
        "def run(*, guarded=False, alternative=True):\n"
        "    if not guarded:\n"
        "        raise RuntimeError('reported failure')\n"
        "    return True\n",
        encoding="utf-8",
    )
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_core.py").write_text(
        "from src.core import run\n\n"
        "def _report_result(result):\n"
        "    print(f'core.run result={result}')\n"
        "    return result\n\n"
        "def _assert_default_contract():\n"
        "    assert _report_result(run()) is True\n\n"
        "def test_reported_failure():\n"
        "    _assert_default_contract()\n\n"
        "def test_guarded_control():\n"
        "    assert run(guarded=True) is True\n\n"
        "def test_alternative_removed():\n"
        "    run(alternative=False)\n",
        encoding="utf-8",
    )
    (workspace / "repro.txt").write_text("captured reproduction\n", encoding="utf-8")
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "tests@example.invalid")
    _git(workspace, "config", "user.name", "Tests")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "reproduced problem")
    return _git(workspace, "rev-parse", "HEAD")


def _research_claims(revision: str) -> dict[str, object]:
    return {
        "research_schema_version": 3,
        "case_id": "case:causal-acceptance",
        "problem_id": "problem:causal-acceptance",
        "repo_revision": revision,
        "research_method": "reproduction",
        "reproduction_status": "reproduced",
        "research_status": "evidence_sufficient",
        "writes_used": False,
        "writes_purpose": ["none"],
        "implementation_performed": False,
        "diff_classification": "allowed_research_edits",
        "artifact_refs": [
            {"artifact_id": "artifact:repro", "kind": "repro", "path": "repro.txt"},
            {"artifact_id": "artifact:source", "kind": "source", "path": "src/core.py"},
        ],
        "experiments": [
            {
                "experiment_id": "experiment:original",
                "scenario_kind": "original_replay",
                "addresses_atom_ids": ["atom:origin"],
                "command": ORIGINAL_COMMAND,
                "result": "The original scenario fails at core.run.",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "verification_boundary": {
                    "boundary_kind": "repository_original_scenario",
                    "requires_live_verification": False,
                    "faithful_equivalence": True,
                    "rationale": (
                        "The exact immutable source command is the complete local behavior and "
                        "the retained repository assertion is its post-change oracle."
                    ),
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
            {
                "experiment_id": "experiment:control",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "experiment:original",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "guard enabled",
                    "expected_difference": "The guarded call returns the expected value.",
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": "python -m pytest -q tests/test_core.py::test_guarded_control",
                "result": "The guarded control passes.",
                "outcome": "refutes",
                "exit_code": 0,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 0,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
            {
                "experiment_id": "experiment:challenge",
                "scenario_kind": "control",
                "control_relationship": {
                    "supports_experiment_id": "experiment:original",
                    "mechanism_symbols": ["core.run"],
                    "controlled_variable": "alternative input disabled",
                    "expected_difference": (
                        "The failure disappears only if that alternative is causal."
                    ),
                },
                "addresses_atom_ids": ["atom:origin"],
                "command": "python -m pytest -q tests/test_core.py::test_alternative_removed",
                "result": "The failure survives removal of the alternative.",
                "outcome": "supports",
                "exit_code": 1,
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
                "artifact_refs": ["artifact:repro", "artifact:source"],
            },
        ],
        "inspected_files": ["src/core.py"],
        "inspected_symbols": ["core.run"],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "hypothesis:missing-default-path",
                "statement": (
                    "core.run raises instead of returning its required value on the default path."
                ),
                "supporting_evidence": ["experiment:original", "experiment:challenge"],
                "counterevidence": ["experiment:control"],
                "mechanism_symbols": ["core.run"],
                "disposition": "primary",
                "disposition_evidence": ["experiment:original", "experiment:control"],
                "falsification_attempts": [
                    {
                        "attempt_id": "attempt:alternative-cause",
                        "hypothesis_id": "hypothesis:missing-default-path",
                        "claim": (
                            "core.run raises instead of returning its required value "
                            "on the default path."
                        ),
                        "baseline_experiment_id": "experiment:original",
                        "challenge_experiment_id": "experiment:challenge",
                        "disproof_condition": {
                            "source": "exit_code",
                            "operator": "equals",
                            "expected": 0,
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "root_cause_confidence": 0.9,
        "broader_class_assessment": "unknown",
        "case_relation_assessment": {
            "disposition": "retain",
            "rationale": "The signed occurrence and verified mechanism form one work unit.",
            "facets": [],
            "material_unknowns": [],
        },
        "actionability_assessment": {
            "disposition": "requires_change",
            "rationale": (
                "The pinned revision still raises on the original default-path replay, "
                "while the guarded control verifies the corrective mechanism."
            ),
            "evidence_refs": ["experiment:original", "experiment:control"],
        },
        "material_unknowns": [],
        "blocking_reasons": [],
        "evidence_boundaries": [],
    }


def _problem_payload(tmp_path: Path) -> dict[str, object]:
    origin = tmp_path / "origin.json"
    origin.write_text('{"failure": true}\n', encoding="utf-8")
    atom = {
        "atom_id": "atom:origin",
        "text": "Default core.run raises instead of returning the required value.",
        "command": ORIGINAL_COMMAND,
        "exit_code": 1,
        "evidence_role": "observation",
        "origin_stage": "runtime",
    }
    atom_snapshot = source_evidence_atom_projection(atom)
    assignment: dict[str, object] = {
        "status": "complete",
        "errors": [],
        "case_id": "case:causal-acceptance",
        "problem_id": "problem:causal-acceptance",
        "expected_atom_ids": ["atom:origin"],
        "atom_receipts": [
            {
                "atom_id": "atom:origin",
                "atom_sha256": sha256(
                    json.dumps(
                        atom_snapshot, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                "atom_snapshot": atom_snapshot,
                "artifact_receipts": [
                    {
                        "path": str(origin),
                        "sha256": sha256(origin.read_bytes()).hexdigest(),
                        "size_bytes": origin.stat().st_size,
                    }
                ],
            }
        ],
    }
    assignment["assignment_sha256"] = evidence_assignment_sha256(assignment)
    return {
        "case_id": "case:causal-acceptance",
        "problem_id": "problem:causal-acceptance",
        "evidence_atoms": [atom],
        "evidence_assignment": assignment,
    }


def _run_stage_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, dict[str, object]]:
    guidance = tmp_path / "configs" / "backlog_stage_guidance" / "repro_research.md"
    guidance.parent.mkdir(parents=True)
    guidance.write_text("# Research deeply\n", encoding="utf-8")
    workspace = tmp_path / "owner"
    revision = _init_reproduced_problem(workspace)
    claims = _research_claims(revision)

    def fake_run_once(*, config: RunnerConfig, request: RunRequest) -> RunResult:
        assert request.keep_workspace is True
        run_dir = tmp_path / "research-agent-run"
        run_dir.mkdir()
        _write_json(
            run_dir / "report.json",
            {
                "schema_version": 1,
                "kind": "troubleshoot_v1",
                "status": "success",
                "goal": "Establish the causal mechanism.",
                "failure_point": "core.run default path",
                "evidence": {"what_happened": "The default path raises."},
                "attempted_fixes": [],
                "recommended_fix_path": ["Correct the default return path."],
                "extensions": {"backlog_repro_research": claims},
            },
        )
        _write_json(run_dir / "diff_numstat.json", [])
        _write_json(
            run_dir / "target_ref.json",
            {
                "commit_sha": revision,
                "ref": request.ref,
                "agent": request.agent,
            },
        )
        _write_json(
            run_dir / "workspace_ref.json",
            {"workspace_dir": str(request.resume_workspace_dir)},
        )
        events = [
            {
                "type": "run_command",
                "data": {"command": ORIGINAL_COMMAND, "exit_code": 1},
            },
            {
                "type": "run_command",
                "data": {
                    "command": "python -m pytest -q tests/test_core.py::test_alternative_removed",
                    "exit_code": 1,
                },
            },
            {
                "type": "run_command",
                "data": {
                    "command": "python -m pytest -q tests/test_core.py::test_guarded_control",
                    "exit_code": 0,
                },
            },
            {
                "type": "read_file",
                "data": {
                    "path": "src/core.py",
                    "bytes": (workspace / "src" / "core.py").stat().st_size,
                    "read_source": "tool",
                    "source_exit_code": 0,
                    **observed_read_attestation(
                        path=workspace / "src" / "core.py",
                        observed_text=(workspace / "src" / "core.py").read_text(encoding="utf-8"),
                        source_exit_code=0,
                        allow_partial=True,
                    ),
                },
            },
        ]
        assert request.resume_workspace_dir is not None
        assigned_index = (
            request.resume_workspace_dir
            / ".usertest_research"
            / "origin_evidence"
            / "assigned"
            / "index.json"
        )
        assert assigned_index.is_file()
        events.append(
            {
                "type": "read_file",
                "data": {
                    "path": assigned_index.relative_to(
                        request.resume_workspace_dir
                    ).as_posix(),
                    "read_source": "tool",
                    "source_exit_code": 0,
                    **observed_read_attestation(
                        path=assigned_index,
                        observed_text=assigned_index.read_text(encoding="utf-8"),
                        source_exit_code=0,
                        allow_partial=False,
                    ),
                },
            }
        )
        (run_dir / "normalized_events.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        return RunResult(run_dir=run_dir, exit_code=0, report_validation_errors=[])

    monkeypatch.setattr(research_runner, "run_once", fake_run_once)
    config = RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        agents={},
        policies={},
    )
    result = research_runner.run_repro_research_stage(
        repo_root=tmp_path,
        repo_input=str(workspace),
        repo_ref="HEAD",
        target_slug="causal_acceptance",
        selected_problems=[_problem_payload(tmp_path)],
        artifacts_dir=tmp_path / "compiled" / "backlog_artifacts",
        agent="claude",
        model=None,
        cfg=config,
        dry_run=False,
        replay_executor=TrustedHostReplayExecutor(
            approved_source_roots=[workspace],
            source_identity=workspace,
        ),
        replay_executor_metadata={
            "executor": "trusted_host",
            "approved_source_roots": [str(workspace.resolve())],
            "source_identity": str(workspace.resolve()),
        },
    )
    dossier = result["items"][0]
    assert isinstance(dossier, dict)
    return workspace, revision, dossier


def test_real_causal_evidence_reaches_durable_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carry real stage-3 receipts to a correct post-merge behavioral outcome."""
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider")
    workspace, revision, dossier = _run_stage_three(tmp_path, monkeypatch)
    assert dossier["research_status"] == "evidence_sufficient", dossier["evidence_verification"][
        "errors"
    ]
    persisted_path = tmp_path / "persisted-research.json"
    _write_json(persisted_path, dossier)
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))

    research_ready, research_reasons = assess_research_readiness(persisted)
    assert research_ready is True, research_reasons
    assert research_reasons == []
    assert verify_persisted_research_evidence(persisted) == (True, [])
    verification = persisted["evidence_verification"]
    assert verification["status"] == "verified"
    assert verification["failure_paths"]
    assert verification["mechanism_evidence"]
    assert verification["outcome_oracles"]
    assert len(verification["verification_boundaries"]) == 1
    assert (
        verification["verification_boundaries"][0]["equivalence_proof"]["equivalence_mode"]
        == "exact_origin_scenario_identity"
    )
    tampered = json.loads(json.dumps(persisted))
    tampered["evidence_assignment"]["atom_receipts"][0]["atom_snapshot"]["command"] = (
        "python -m pytest tests/test_unrelated.py"
    )
    tampered_required, tampered_reasons = infer_live_verification_requirement(
        {},
        tampered,
    )
    assert tampered_required is False
    assert tampered_reasons == ["verification_boundary_unverified_legacy"]
    assert len(verification["falsification_interventions"]) == 1
    intervention_receipt = verification["falsification_interventions"][0]
    assert intervention_receipt["controlled_input_difference"]["difference_count"] == 1
    assert intervention_receipt["controlled_input_difference"]["difference"]["slot"] == (
        "keyword:alternative"
    )
    assert intervention_receipt["observed_polarity"]["polarity"] == (
        "failure_persists_after_intervention"
    )
    assert (
        verification["hypothesis_refs"][0]["falsification_attempts"][0]["intervention_receipt_id"]
        == intervention_receipt["intervention_receipt_id"]
    )
    assert revision == persisted["repo_revision"]
    assert workspace.is_dir()

    hypothesis = persisted["root_cause_hypotheses"][0]
    failure_path = verification["failure_paths"][0]
    mechanism_evidence = next(
        item
        for item in verification["mechanism_evidence"]
        if item["hypothesis_id"] == hypothesis["hypothesis_id"]
    )
    intervention = "Return the required successful value from core.run's verified default path."
    option = {
        "case_id": persisted["case_id"],
        "problem_id": persisted["problem_id"],
        "option_id": "option:correct-default-path",
        "family_id": "causal_mechanism",
        "summary": "Correct the demonstrated default branch in core.run.",
        "tradeoffs": "The default branch changes from raising to its documented return value.",
        "recurrence_prevention": "The original replay asserts the positive return value.",
        "change_surface_hypothesis": "Modify only the verified core.run control point.",
        "test_implications": "The original replay must report one passing test.",
        "rationale": "A controlled replay isolates the default branch as the mechanism.",
        "causal_coverage": {
            "mechanism_addressed": hypothesis["statement"],
            "research_binding": {
                "hypothesis_id": hypothesis["hypothesis_id"],
                "hypothesis_statement": hypothesis["statement"],
                "mechanism_symbols": hypothesis["mechanism_symbols"],
                "supporting_evidence_refs": hypothesis["supporting_evidence"],
                "counterevidence_refs": hypothesis["counterevidence"],
                "falsification_attempt_refs": [
                    attempt["attempt_id"] for attempt in hypothesis["falsification_attempts"]
                ],
                "deterministic_closure_refs": [],
                "intervention_points": [
                    {
                        "mechanism_symbol": "core.run",
                        "target_path": "src/core.py",
                        "target_symbol": "core.run",
                        "intervention": intervention,
                    }
                ],
            },
            "symptoms_covered": ["The original default call raises."],
            "unsupported_assumptions": [],
            "residual_recurrence_paths": [],
            "compatibility_risks": [],
            "testability": {
                "before": "The exact original replay fails.",
                "after": "The exact replay passes and asserts True.",
            },
            "outcome_strategy": {
                "intended_operation": (
                    "The default core.run call returns its required True value."
                ),
                "success_properties": [
                    "The unchanged original replay passes its existing return-value assertion."
                ],
                "safety_constraints": [
                    "The guarded core.run call continues to return True."
                ],
                "post_change_replay_mode": "verified_fail_first",
                "original_scenario_experiment_ids": ["experiment:original"],
            },
        },
        "scope_evidence": {
            "scope_level": "single_path",
            "independent_consumers_or_failure_paths": [
                {
                    "name": failure_path["path_name"],
                    "evidence_refs": [failure_path["failure_path_id"]],
                }
            ],
        },
    }
    option_ready, option_reasons = assess_solution_option_readiness(
        option,
        research=persisted,
    )
    assert option_ready is True, option_reasons
    positive_contract_id = verification["outcome_oracles"][0]["positive_outcome_contracts"][0][
        "positive_outcome_contract_id"
    ]

    falsification = bind_falsification_review(
        {
            "problem_id": persisted["problem_id"],
            "selected_option_id": option["option_id"],
            "verdict": "accept",
            "strongest_counterargument": (
                "The guard, rather than the default branch, could explain the failure."
            ),
            "evidence_refs": [
                {
                    "ref": mechanism_evidence["mechanism_evidence_id"],
                    "finding": (
                        "The controlled guard changes the outcome while the adversarial "
                        "challenge leaves the default-path failure intact."
                    ),
                    "effect": "limits_scope",
                }
            ],
            "unsupported_assumptions": [],
            "residual_risks": [],
            "critical_findings": [],
            "material_risk_dispositions": [],
            "evidence_that_would_change_verdict": (
                "A clean replay showing the same failure outside core.run."
            ),
            "selected_positive_outcome_contract_id": positive_contract_id,
            "outcome_contract_reviews": [
                {
                    "positive_outcome_contract_id": positive_contract_id,
                    "verdict": "sufficient",
                    "semantic_relation_assessment": (
                        "The fail-first assertion invokes core.run on the same source "
                        "scenario and checks its required successful return."
                    ),
                    "proves_intended_operation": True,
                    "problem_coverage": "full",
                    "residual_untested_paths": [],
                    "evidence_refs": [mechanism_evidence["mechanism_evidence_id"]],
                }
            ],
            "outcome_strategy_review": {
                "verdict": "sufficient",
                "semantic_relation_assessment": (
                    "The strategy requires the useful True return on the unchanged "
                    "verified fail-first replay, not merely removal of the exception."
                ),
                "proves_intended_operation": True,
                "problem_coverage": "full",
                "residual_untested_paths": [],
                "evidence_refs": [mechanism_evidence["mechanism_evidence_id"]],
            },
        },
        problem_id=persisted["problem_id"],
        selected_option=option,
        research=persisted,
    )
    selection = {
        "case_id": persisted["case_id"],
        "problem_id": persisted["problem_id"],
        "selected_option_id": option["option_id"],
        "selected_family_id": option["family_id"],
        "selection_rationale": "It changes the runner-verified causal control point.",
        "repo_intent_alignment": "It preserves the existing public function contract.",
        "why_other_options_were_not_selected": "No other mechanism is evidenced.",
        "needs_ux_review": False,
        "causal_coverage_evaluation": {
            "mechanism_fit": "Exact symbol and branch demonstrated by replay and control.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": False,
        },
        "falsification_review": falsification,
        "change_surface": {"user_visible": False, "kinds": ["internal"]},
    }
    selection_ready, selection_reasons = assess_selection_readiness(
        selection,
        options=[option],
        research=persisted,
    )
    assert selection_ready is True, selection_reasons

    problem = {
        "case_id": persisted["case_id"],
        "problem_id": persisted["problem_id"],
        "canonical_problem_id": persisted["problem_id"],
        "case_member_problem_ids": [persisted["problem_id"]],
        "title": "core.run fails its default return contract",
        "problem": "The default call raises before producing the required value.",
        "user_impact": "Callers cannot complete the operation.",
    }
    original = next(
        item for item in persisted["experiments"] if item["experiment_id"] == "experiment:original"
    )
    target = {
        "action": "modify",
        "path": "src/core.py",
        "symbols": ["core.run"],
        "change": intervention,
    }
    plan: dict[str, object] = {
        "change_plan_id": "plan:correct-default-path",
        "case_id": persisted["case_id"],
        "problem_id": persisted["problem_id"],
        "selected_option_id": option["option_id"],
        "title": "Correct core.run's default path",
        "problem": problem["problem"],
        "user_impact": problem["user_impact"],
        "proposed_fix": intervention,
        "repo_revision": persisted["repo_revision"],
        "change_targets": [target],
        "implementation_steps": [
            "Change `core.run` so its default branch returns the required True value."
        ],
        "verification_steps": [
            "Replay the exact stage-3 original scenario and require the positive assertion."
        ],
        "verification_commands": [ORIGINAL_COMMAND],
        "outcome_verification_roles": {
            "original_scenario": {
                "description": "Replay the exact stage-3 scenario.",
                "research_experiment_id": original["experiment_id"],
                "commands": [ORIGINAL_COMMAND],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0},
                ],
            },
            "live": None,
            "mitigation_effect": None,
            "recurrence": {
                "description": "Inspect a later canonical evidence window for recurrence.",
                "verification_owner": "centralized_case_refresh",
                "commands": [],
                "predicates": [],
            },
        },
        "before_after_reproduction": {
            "original_scenario": "Replay the exact failing default call.",
            "research_experiment_id": original["experiment_id"],
            "expected_outcome_state": "resolved",
            "before_change": {
                "command": original["command"],
                "expected_exit_code": original["exit_code"],
                "expected_result": original["result"],
                "observable_assertion": original["observable_assertion"],
            },
            "after_change": {
                "command": original["command"],
                "expected_exit_code": 0,
                "expected_result": "The default call returns True and the test passes.",
                "observable_assertions": [
                    {"source": "exit_code", "operator": "equals", "expected": 0},
                    {
                        "source": "stdout",
                        "operator": "contains",
                        "expected": "core.run result=True",
                    },
                ],
            },
            "proof_limitation": None,
            "alternate_verification": None,
        },
        "compatibility_and_failure_modes": {
            "preserved_behaviors": ["The guarded call continues to return True."],
            "intentional_changes": ["The default call returns instead of raising."],
            "failure_modes": ["Unexpected callers still receive ordinary exceptions."],
            "migration_required": False,
        },
        "causal_coverage": option["causal_coverage"],
        "scope_evidence": option["scope_evidence"],
        "requires_live_verification": False,
        "live_verification_rationale": "The complete behavior is reproduced locally.",
        "success_criteria": ["The original replay prints one passed test."],
        "rollback_notes": "Revert the core.run branch change.",
        "suggested_owner": "core",
        "related_change_plan_ids": [],
    }
    plan["target_contract"] = build_plan_target_contract(plan, repo_root=workspace)
    plan = assign_plan_revision_id(
        bind_plan_outcome_oracle(plan, research=persisted, selection=selection)
    )
    selection_for_plan = {**selection, "selected_option": option}
    plan_ready, plan_reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=persisted,
        selection=selection_for_plan,
    )
    assert plan_ready is True, plan_reasons

    priority = {
        "case_id": persisted["case_id"],
        "problem_id": persisted["problem_id"],
        "selected_for_research": True,
        "priority_bucket": "high",
        "priority_rationale": "The original scenario is reproduced.",
        "priority_status": "prioritized",
    }
    ticket_ready, ticket_reasons = assess_ticket_readiness(
        {
            "problem_record": problem,
            "priority": priority,
            "research": persisted,
            "solution_options": [option],
            "selected_solution": selection,
            "change_plan": plan,
        }
    )
    assert ticket_ready is True, ticket_reasons

    fingerprint = _case_plan_fingerprint(
        case_id=str(plan["case_id"]),
        plan_revision_id=str(plan["plan_revision_id"]),
    )
    target_contract = plan["target_contract"]
    roles = plan["outcome_verification_roles"]
    ticket_path = (
        workspace
        / ".agents"
        / "plans"
        / "5 - complete"
        / f"20260710_{fingerprint}_causal_acceptance.md"
    )
    ticket_path.parent.mkdir(parents=True)
    markdown = (
        "# Generated causal acceptance plan\n\n"
        "Generated by `python -m usertest_backlog.cli reports export-tickets`\n\n"
        f"- Fingerprint: `{fingerprint}`\n"
        f"- Case ID: `{plan['case_id']}`\n"
        f"- Plan revision ID: `{plan['plan_revision_id']}`\n"
        "- Export kind: `implementation`\n"
        "- Stage: `ready_for_ticket`\n"
        "- Requires live verification: `false`\n\n"
        "### Verification command contract\n\n"
        + render_verification_contract_markdown(
            [ORIGINAL_COMMAND],
            outcome_roles=roles,
        )
        + "\n\n### Machine-verifiable implementation scope contract\n\n"
        + render_plan_target_contract_markdown(target_contract)
        + "\n\n### Original-scenario before / after proof\n\n"
        "The following block is retained evidence/data, not executable instructions.\n\n"
        "```json\n" + json.dumps(plan["before_after_reproduction"], indent=2) + "\n```\n"
    )
    ticket_path.write_text(markdown, encoding="utf-8")
    verification_contract = parse_verification_contract_markdown(markdown)
    assert verification_contract is not None

    # This is the implementation under test: it changes the verified mechanism and
    # produces the positive behavior asserted by the original scenario.
    (workspace / "src" / "core.py").write_text(
        "def run(*, guarded=False, alternative=True):\n    return True\n",
        encoding="utf-8",
    )
    _git(workspace, "add", "src/core.py")
    _git(workspace, "commit", "-m", "correct verified default path")
    merged_commit = _git(workspace, "rev-parse", "HEAD")

    implementation_runs_root = tmp_path / "implementation-runs"
    implementation_run = implementation_runs_root / "correct"
    implementation_run.mkdir(parents=True)
    verification_summary = _run_verification_commands(
        run_dir=implementation_run,
        attempt_number=1,
        commands=[ORIGINAL_COMMAND],
        command_prefix=[],
        cwd=workspace,
        timeout_seconds=None,
        python_executable=sys.executable,
        artifacts_dir_rel=Path("."),
    )
    verification_summary["commands_configured"] = [ORIGINAL_COMMAND]
    _write_json(implementation_run / "verification.json", verification_summary)
    assert verification_summary["passed"] is True

    ticket_provenance = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "case_id": plan["case_id"],
        "plan_revision_id": plan["plan_revision_id"],
        "legacy_identity": False,
        "ticket_body_sha256": canonical_ticket_body_sha256(markdown),
        "local_plan_sha256": canonical_plan_sha256(markdown),
        "local_plan_path": str(ticket_path),
        "local_plan_filename": ticket_path.name,
        "verification_contract": verification_contract,
        "verification_contract_sha256": verification_contract["contract_sha256"],
        "target_contract": target_contract,
        "target_contract_sha256": target_contract["contract_sha256"],
        "generated_ticket": True,
    }
    stored_provenance = {
        key: ticket_provenance[key]
        for key in (
            "schema_version",
            "fingerprint",
            "case_id",
            "plan_revision_id",
            "legacy_identity",
            "ticket_body_sha256",
            "local_plan_sha256",
            "local_plan_path",
            "local_plan_filename",
            "verification_contract_sha256",
            "target_contract_sha256",
            "generated_ticket",
        )
    }
    binding = build_verification_binding(
        ticket_provenance=ticket_provenance,
        configured_commands=[ORIGINAL_COMMAND],
    )
    _write_json(
        implementation_run / "ticket_ref.json",
        {
            "schema_version": 2,
            "fingerprint": fingerprint,
            "case_id": plan["case_id"],
            "plan_revision_id": plan["plan_revision_id"],
            "ticket_provenance": stored_provenance,
            "verification_binding": binding,
            "owner_repo": {
                "root": str(workspace.resolve()),
                "idea_path": str(ticket_path),
            },
        },
    )
    test_receipt = validate_bound_runner_verification(
        run_dir=implementation_run,
        fingerprint=fingerprint,
        case_id=str(plan["case_id"]),
        plan_revision_id=str(plan["plan_revision_id"]),
        evidence_kind="test",
        owner_root=workspace,
        trusted_runs_root=implementation_runs_root,
        expected_ticket_provenance=ticket_provenance,
    )
    durable_provenance = {
        **ticket_provenance,
        "verified_implementation_head": merged_commit,
    }
    outcome = validate_outcome_record(
        {
            "schema_version": 1,
            "case_id": plan["case_id"],
            "plan_revision_id": plan["plan_revision_id"],
            "state": "tests_verified",
            "recorded_at": "2026-07-10T12:00:00Z",
            "requires_live_verification": False,
            "target_branch": "dev",
            "merged_commit": merged_commit,
            "pr_url": "https://example.invalid/pr/causal-acceptance",
            "test_evidence": [
                {
                    "kind": "runner_verification",
                    "reference": test_receipt["verification_path"],
                    "result": "passed",
                    "runner_receipt": test_receipt,
                }
            ],
            "ci_evidence": [],
            "original_scenario_evidence": [],
            "live_evidence": [],
            "mitigation_evidence": [],
            "remaining_risks": ["Original failure scenario has not been replayed after merge"],
            "recurrence_check": {"status": "not_run", "evidence": []},
            "ticket_provenance": durable_provenance,
        }
    )
    ticket_path.write_text(upsert_outcome_markdown(markdown, outcome), encoding="utf-8")
    tool_root = tmp_path / "tool"
    ledger_path = tool_root / ".agents" / "state" / "backlog_implement_actions.yaml"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "updated_at": None,
                "actions": {
                    fingerprint: {
                        "fingerprint": fingerprint,
                        "last_outcome_state": "tests_verified",
                        "outcome": outcome,
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    # This acceptance fixture owns the causal Stage 3-to-outcome chain, but does
    # not synthesize the independent PR review/merge receipts. Their terminal
    # provenance gate is covered end to end in backlog_repo and progression
    # tests, so isolate that separate boundary without weakening production.
    monkeypatch.setattr(
        outcome_progression,
        "_require_terminal_outcome_provenance",
        lambda **_: None,
    )
    progression = progress_post_merge_outcome(
        repo_root=tool_root,
        owner_root=workspace,
        ticket_path=ticket_path,
        ledger_path=ledger_path,
    )
    final_outcome = extract_outcome_markdown(ticket_path.read_text(encoding="utf-8"))
    assert progression.complete is True, progression.detail
    assert progression.roles_run == ("original_scenario",)
    assert final_outcome is not None
    assert final_outcome["state"] == "resolved"
    assert final_outcome["original_scenario_evidence"][0]["result"] == "passed"
    role_artifacts = list(
        (tool_root / "runs" / "usertest_implement" / "_outcome_roles").rglob("outcome_role.json")
    )
    assert len(role_artifacts) == 1
    role_artifact = json.loads(role_artifacts[0].read_text(encoding="utf-8"))
    assert role_artifact["timeout_seconds"] is None
    assert role_artifact["passed"] is True
    assert any(
        result["predicate"]["type"] == "command_exit_code" and result["passed"] is True
        for result in role_artifact["predicate_results"]
    )
    assert role_artifact["positive_contract_source_receipts"] == [
        {
            "positive_outcome_contract_id": verification["outcome_oracles"][0][
                "positive_outcome_contracts"
            ][0]["positive_outcome_contract_id"],
            "path": "tests/test_core.py",
            "expected_sha256": sha256(
                (workspace / "tests" / "test_core.py").read_bytes()
            ).hexdigest(),
            "observed_sha256": sha256(
                (workspace / "tests" / "test_core.py").read_bytes()
            ).hexdigest(),
            "observed_test_function_source_sha256": verification["outcome_oracles"][0][
                "positive_outcome_contracts"
            ][0]["repository_contract"]["test_function_source_sha256"],
            "observed_reachable_function_contracts": verification["outcome_oracles"][0][
                "positive_outcome_contracts"
            ][0]["repository_contract"]["reachable_function_contracts"],
            "observed_relevant_module_imports_sha256": verification["outcome_oracles"][0][
                "positive_outcome_contracts"
            ][0]["repository_contract"]["relevant_module_imports_sha256"],
            "test_function_source_sha256": verification["outcome_oracles"][0][
                "positive_outcome_contracts"
            ][0]["repository_contract"]["test_function_source_sha256"],
            "status": "verified",
        }
    ]

    # Unrelated module edits are allowed: the contract binds only the selected
    # test's reachable AST closure and the imports that closure consumes.
    test_path = workspace / "tests" / "test_core.py"
    original_test = test_path.read_text(encoding="utf-8")
    test_path.write_text(
        "import decimal\n" + original_test + "\n# unrelated comment\n"
        "def test_unrelated_addition():\n    assert decimal.Decimal('1') == 1\n",
        encoding="utf-8",
    )
    scoped_receipts = outcome_roles._verify_positive_contract_sources(
        verification["outcome_oracles"][0],
        workspace=workspace,
    )
    assert scoped_receipts[0]["status"] == "verified"
    test_path.write_text(original_test, encoding="utf-8")

    # The implementation cannot weaken a reachable helper assertion that granted
    # semantic readiness while leaving the selected test function unchanged.
    test_path.write_text(
        original_test.replace("assert _report_result(run()) is True", "assert True", 1),
        encoding="utf-8",
    )
    original_role = verification_contract["outcome_roles"]["original_scenario"]
    with pytest.raises(
        ValueError,
        match="outcome_role_repository_contract_source_changed",
    ):
        run_outcome_evidence_role(
            workspace=workspace,
            output_path=tmp_path / "tampered-original-scenario.json",
            role="original_scenario",
            role_contract=original_role,
            case_id=str(plan["case_id"]),
            plan_revision_id=str(plan["plan_revision_id"]),
            merged_commit=merged_commit,
            verification_contract_sha256=verification_contract["contract_sha256"],
            target_contract_sha256=target_contract["contract_sha256"],
            verified_implementation_head=merged_commit,
            timeout_seconds=None,
        )
    test_path.write_text(original_test, encoding="utf-8")
    assert "tests/test_core.py" not in _git(workspace, "status", "--porcelain")
