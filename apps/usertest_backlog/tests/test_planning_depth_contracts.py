from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from backlog_core import (
    assign_plan_revision_id,
    bind_falsification_review,
)
from backlog_miner.pipeline import load_pipeline_prompt_manifest

import usertest_backlog.workflows.depth_contracts as depth_contracts
from usertest_backlog.workflows.depth_contracts import (
    assess_repo_grounding,
    change_plan_quality_errors,
    falsification_review_errors,
    parse_optioning_response,
    selection_quality_errors,
)
from usertest_backlog.workflows.implementation_planning import (
    _run_implementation_planning_stage,
)
from usertest_backlog.workflows.solution_options import _run_solution_optioning_stage
from usertest_backlog.workflows.solution_selection import _run_solution_selection_stage


@pytest.mark.parametrize(
    "symbol",
    [
        "settings.DEFAULT_TIMEOUT",
        "settings.Config.mode",
        "settings.json_loader",
        "settings.alias_name",
    ],
)
def test_planner_symbol_grounding_accepts_python_bindings(symbol: str) -> None:
    content = (
        "import json as json_loader\n"
        "from source import original as alias_name\n"
        "DEFAULT_TIMEOUT: int = 30\n"
        "class Config:\n"
        "    mode = 'safe'\n"
    )

    assert depth_contracts._target_symbol_exists(
        path=Path("src/settings.py"),
        content=content,
        symbol=symbol,
    )


@pytest.mark.parametrize(
    ("path", "content", "symbol"),
    [
        (Path("config.json"), '{"tool":{"a/b":{"~key":true}}}', "config:/tool/a~1b/~0key"),
        (
            Path("pyproject.toml"),
            "[tool.pytest.ini_options]\naddopts = '-q'\n",
            "config:/tool/pytest/ini_options/addopts",
        ),
        (
            Path("pipeline.yaml"),
            "pipelines:\n  - name: backlog\n",
            "config:/pipelines/0/name",
        ),
    ],
)
def test_planner_symbol_grounding_accepts_rfc6901_config_pointers(
    path: Path,
    content: str,
    symbol: str,
) -> None:
    assert depth_contracts._target_symbol_exists(
        path=path,
        content=content,
        symbol=symbol,
    )


@pytest.mark.parametrize(
    ("path", "content", "symbol"),
    [
        (Path("config.json"), '{"tool":1,"tool":2}', "config:/tool"),
        (Path("config.yaml"), "tool: 1\ntool: 2\n", "config:/tool"),
        (Path("config.json"), '{"tool":{"value":1}}', "config:/tool/~2value"),
        (Path("src/constants.ts"), "// RETRY_LIMIT is configured elsewhere\n", "RETRY_LIMIT"),
    ],
)
def test_planner_symbol_grounding_rejects_ambiguous_or_mentioned_only_symbols(
    path: Path,
    content: str,
    symbol: str,
) -> None:
    assert not depth_contracts._target_symbol_exists(
        path=path,
        content=content,
        symbol=symbol,
    )


@pytest.mark.parametrize(
    ("content", "symbol"),
    [
        ("export const RETRY_LIMIT = 3;\n", "RETRY_LIMIT"),
        ("static readonly RetryLimit: number = 3;\n", "RetryLimit"),
        ("MAX_RETRIES = 3\n", "MAX_RETRIES"),
    ],
)
def test_planner_symbol_grounding_accepts_practical_non_python_constants(
    content: str,
    symbol: str,
) -> None:
    assert depth_contracts._target_symbol_exists(
        path=Path("src/constants.ts"),
        content=content,
        symbol=symbol,
    )


def _valid_option(
    *,
    option_id: str = "option:case:most_direct",
    family_id: str = "most_direct",
    mechanism: str = "Normalize the runner result at the report boundary",
    scope_level: str = "single_path",
    paths: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if paths is None:
        research = _valid_option_research()
        verification = research["evidence_verification"]
        assert isinstance(verification, dict)
        failure_path = verification["failure_paths"][0]
        paths = [
            {
                "name": failure_path["path_name"],
                "evidence_refs": [failure_path["failure_path_id"]],
            }
        ]
    return {
        "option_id": option_id,
        "problem_id": "problem:case",
        "family_id": family_id,
        "summary": "Normalize the runner result before report assembly.",
        "tradeoffs": "Keeps the change at the observed boundary.",
        "recurrence_prevention": "All results crossing this boundary are normalized.",
        "change_surface_hypothesis": "packages/runner_core result normalization",
        "test_implications": "Replay the failing result fixture.",
        "rationale": "The trace identifies the report boundary as the failing mechanism.",
        "option_status": "optioned",
        "causal_coverage": {
            "mechanism_addressed": mechanism,
            "research_binding": {
                "hypothesis_id": "h1",
                "hypothesis_statement": (
                    "The runner result reaches report assembly without normalization."
                ),
                "mechanism_symbols": ["runner.build_report"],
                "supporting_evidence_refs": ["exp-1", "exp-challenge"],
                "counterevidence_refs": ["exp-control"],
                "falsification_attempt_refs": ["falsify-h1-alternative"],
                "deterministic_closure_refs": [],
                "intervention_points": [
                    {
                        "mechanism_symbol": "runner.build_report",
                        "target_path": "packages/runner_core/src/runner_core/runner.py",
                        "target_symbol": "runner.build_report",
                        "intervention": "Normalize the result at the verified report boundary.",
                    }
                ],
            },
            "symptoms_covered": ["Malformed runner result reaches report assembly"],
            "unsupported_assumptions": [],
            "residual_recurrence_paths": [],
            "compatibility_risks": [],
            "testability": {
                "before": "Fixture produces the malformed report.",
                "after": "The same fixture produces a classified report.",
            },
        },
        "scope_evidence": {
            "scope_level": scope_level,
            "independent_consumers_or_failure_paths": paths,
        },
    }


def _valid_option_research() -> dict[str, object]:
    control: dict[str, object] = {
        "verification_method": "pytest_ast_controlled_difference_v2",
        "hypothesis_id": "h1",
        "support_experiment_id": "exp-1",
        "control_experiment_id": "exp-control",
        "mechanism_symbols": ["runner.build_report"],
        "controlled_input_difference": {"difference_count": 1},
        "observable_difference": {"difference_kind": "failing_exit_to_zero"},
        "adversarial_effect": "limits_scope",
    }
    control["control_verification_id"] = _test_content_id(
        "control_verification",
        control,
        "control_verification_id",
    )
    consumer_identity = {
        "kind": "production_entrypoint",
        "entrypoint": "runner.build_report",
    }
    independence_key = sha256(
        json.dumps(
            consumer_identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    path: dict[str, object] = {
        "verification_method": "runner_controlled_failure_path_v1",
        "path_name": "runner.build_report",
        "consumer_identity": consumer_identity,
        "independence_key": independence_key,
        "hypothesis_id": "h1",
        "support_experiment_id": "exp-1",
        "support_selection_id": "h1:exp-1",
        "control_verification_id": control["control_verification_id"],
        "mechanism_symbols": ["runner.build_report"],
        "origin_atom_ids": ["atom:runner-report"],
        "observed_failure": {"source": "exit_code", "exit_code": 1},
    }
    path["failure_path_id"] = _test_content_id(
        "failure_path",
        path,
        "failure_path_id",
    )
    mechanism_evidence: dict[str, object] = {
        "evidence_type": "controlled_scenario",
        "hypothesis_id": "h1",
        "mechanism_symbols": ["runner.build_report"],
        "code_paths": [
            {
                "symbol": "runner.build_report",
                "path": "packages/runner_core/src/runner_core/runner.py",
            }
        ],
        "experiment_ids": ["exp-1", "exp-control", "exp-challenge"],
        "artifact_refs": [],
        "origin_atom_ids": ["atom:runner-report"],
        "path_name": "runner.build_report",
        "consumer_identity": consumer_identity,
        "independence_key": independence_key,
        "controlled_condition": {
            "variable": "guard enabled",
            "expected_difference": "The guarded control succeeds.",
        },
        "observable_difference": {"difference_kind": "failing_exit_to_zero"},
        "strong_pytest_control_id": control["control_verification_id"],
        "mechanism_link": None,
        "adversarial_effect": "limits_scope",
    }
    mechanism_evidence["mechanism_evidence_id"] = _test_content_id(
        "mechanism_evidence",
        mechanism_evidence,
        "mechanism_evidence_id",
    )
    intervention: dict[str, object] = {
        "verification_method": "pytest_ast_falsification_intervention_v1",
        "hypothesis_id": "h1",
        "attempt_id": "falsify-h1-alternative",
        "baseline_experiment_id": "exp-1",
        "challenge_experiment_id": "exp-challenge",
        "mechanism_symbols": ["runner.build_report"],
        "controlled_input_difference": {"difference_count": 1},
        "observed_polarity": {
            "polarity": "failure_persists_after_intervention"
        },
    }
    intervention["intervention_receipt_id"] = _test_content_id(
        "falsification_intervention",
        intervention,
        "intervention_receipt_id",
    )
    return {
        "inspected_files": ["packages/runner_core/src/runner_core/runner.py"],
        "inspected_symbols": ["runner.build_report"],
        "experiments": [
            {
                "experiment_id": "exp-1",
                "scenario_kind": "original_replay",
                "command": "pytest tests/test_report.py::test_malformed -q",
                "result": "The malformed report reaches assembly",
                "outcome": "supports",
                "exit_code": 1,
                "addresses_atom_ids": ["atom:runner-report"],
                "observable_assertion": {
                    "source": "stderr",
                    "operator": "contains",
                    "expected": "malformed report",
                },
                "artifact_refs": ["artifact:mechanism"],
            },
            {"experiment_id": "exp-control", "artifact_refs": []},
            {
                "experiment_id": "exp-challenge",
                "scenario_kind": "faithful_replay",
                "command": "pytest tests/test_report.py::test_alternative_removed -q",
                "result": "The malformed report remains after removing the alternative",
                "outcome": "supports",
                "exit_code": 1,
                "addresses_atom_ids": ["atom:runner-report"],
                "observable_assertion": {
                    "source": "stderr",
                    "operator": "contains",
                    "expected": "malformed report",
                },
                "artifact_refs": ["artifact:mechanism"],
            },
        ],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "h1",
                "statement": (
                    "The runner result reaches report assembly without normalization."
                ),
                "mechanism_symbols": ["runner.build_report"],
                "supporting_evidence": ["exp-1", "exp-challenge"],
                "counterevidence": ["exp-control"],
                "falsification_attempts": [
                    {
                        "attempt_id": "falsify-h1-alternative",
                        "hypothesis_id": "h1",
                        "claim": (
                            "The runner result reaches report assembly without normalization."
                        ),
                        "baseline_experiment_id": "exp-1",
                        "challenge_experiment_id": "exp-challenge",
                        "disproof_condition": {
                            "source": "stderr",
                            "operator": "not_contains",
                            "expected": "malformed report",
                        },
                        "outcome": "survived",
                    }
                ],
            }
        ],
        "evidence_verification": {
            "status": "verified",
            "receipt_sha256": "a" * 64,
            "inspected_symbols": [
                {
                    "symbol": "runner.build_report",
                    "path": "packages/runner_core/src/runner_core/runner.py",
                }
            ],
            "control_verifications": [control],
            "failure_paths": [path],
            "mechanism_evidence": [mechanism_evidence],
            "falsification_interventions": [intervention],
            "deterministic_mechanism_closures": [],
            "experiments": [
                {
                    "experiment_id": "exp-1",
                    "command": "pytest tests/test_report.py::test_malformed -q",
                    "declared_result": "The malformed report reaches assembly",
                    "exit_code": 1,
                    "outcome": "supports",
                    "scenario_kind": "original_replay",
                    "observable_assertion": {
                        "source": "stderr",
                        "operator": "contains",
                        "expected": "malformed report",
                    },
                    "assertion_passed": True,
                    "stdout_sha256": "1" * 64,
                    "stderr_sha256": "2" * 64,
                },
                {
                    "experiment_id": "exp-challenge",
                    "command": (
                        "pytest tests/test_report.py::test_alternative_removed -q"
                    ),
                    "declared_result": (
                        "The malformed report remains after removing the alternative"
                    ),
                    "exit_code": 1,
                    "outcome": "supports",
                    "scenario_kind": "faithful_replay",
                    "observable_assertion": {
                        "source": "stderr",
                        "operator": "contains",
                        "expected": "malformed report",
                    },
                    "assertion_passed": True,
                    "stdout_sha256": "3" * 64,
                    "stderr_sha256": "4" * 64,
                },
            ],
        },
    }


def _test_content_id(prefix: str, value: dict[str, object], id_field: str) -> str:
    projection = {key: item for key, item in value.items() if key != id_field}
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    return f"{prefix}:{sha256(encoded).hexdigest()}"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _control_id(research: dict[str, object]) -> str:
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    return str(verification["mechanism_evidence"][0]["mechanism_evidence_id"])


def _add_positive_outcome_contract(research: dict[str, object]) -> str:
    verification = research["evidence_verification"]
    assert isinstance(verification, dict)
    evidence_id = str(verification["mechanism_evidence"][0]["mechanism_evidence_id"])
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "repository_test_assertion",
        "research_experiment_id": "exp-1",
        "mechanism_evidence_ids": [evidence_id],
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    contract["positive_outcome_contract_id"] = _test_content_id(
        "positive_outcome_contract",
        contract,
        "positive_outcome_contract_id",
    )
    oracle: dict[str, object] = {
        "schema_version": 1,
        "case_id": "case:case",
        "repo_revision": "abc123",
        "research_experiment_id": "exp-1",
        "scenario_kind": "original_replay",
        "origin_atom_ids": ["atom:runner-report"],
        "mechanism_evidence_ids": [evidence_id],
        "baseline": {
            "exit_code": 1,
            "observable_assertion": {
                "source": "stderr",
                "operator": "contains",
                "expected": "malformed report",
            },
            "stdout_sha256": "1" * 64,
            "stderr_sha256": "2" * 64,
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": ["pytest", "tests/test_report.py::test_malformed", "-q"],
            "command_authorization": {
                "authorization_kind": "declared_inspected_repository_entrypoint",
                "executed_argv_sha256": _canonical_sha256(
                    ["pytest", "tests/test_report.py::test_malformed", "-q"]
                ),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
        "positive_outcome_contracts": [contract],
    }
    oracle["outcome_oracle_id"] = _test_content_id(
        "outcome_oracle", oracle, "outcome_oracle_id"
    )
    verification["outcome_oracles"] = [oracle]
    return str(contract["positive_outcome_contract_id"])


def test_optioning_supports_explicit_zero_option_outcomes() -> None:
    payload = {
        "problem_id": "problem:case",
        "optioning_status": "insufficient_evidence",
        "decision_rationale": "The original stderr artifact is unavailable.",
        "options": [],
    }

    outcome, options, warnings = parse_optioning_response(
        json.dumps(payload),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct", "most_robust", "most_comprehensive"},
        research_dossier=_valid_option_research(),
    )

    assert outcome["optioning_status"] == "insufficient_evidence"
    assert outcome["option_count"] == 0
    assert options == []
    assert warnings == []


def test_optioning_rejects_rhetorical_duplicate_mechanisms() -> None:
    direct = _valid_option()
    robust = _valid_option(
        option_id="option:case:most_robust",
        family_id="most_robust",
    )
    payload = {
        "problem_id": "problem:case",
        "optioning_status": "options_produced",
        "decision_rationale": "Two family labels were considered.",
        "options": [direct, robust],
    }

    outcome, options, warnings = parse_optioning_response(
        json.dumps(payload),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct", "most_robust", "most_comprehensive"},
        research_dossier=_valid_option_research(),
    )

    assert outcome["option_count"] == 1
    assert outcome["rejected_option_count"] == 1
    assert [option["option_id"] for option in options] == ["option:case:most_direct"]
    assert any("duplicate_mechanism" in warning for warning in warnings)


def test_optioning_rejects_reordered_mechanism_paraphrase() -> None:
    direct = _valid_option()
    paraphrase = _valid_option(
        option_id="option:case:paraphrase",
        mechanism="At the report boundary normalize the result from the runner",
    )
    outcome, options, warnings = parse_optioning_response(
        json.dumps(
            {
                "optioning_status": "options_produced",
                "decision_rationale": "Two wordings were offered.",
                "options": [direct, paraphrase],
            }
        ),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct"},
        research_dossier=_valid_option_research(),
    )

    assert outcome["option_count"] == 1
    assert [option["option_id"] for option in options] == [direct["option_id"]]
    assert any("duplicate_mechanism" in warning for warning in warnings)


def test_optioning_allows_distinct_mechanisms_in_same_optional_family() -> None:
    first = _valid_option()
    second = _valid_option(
        option_id="option:case:alternate-direct",
        mechanism="Reject malformed results before they enter report assembly",
    )
    payload = {
        "optioning_status": "options_produced",
        "decision_rationale": "Both local mechanisms are independently supported.",
        "options": [first, second],
    }
    outcome, options, warnings = parse_optioning_response(
        json.dumps(payload),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct"},
        research_dossier=_valid_option_research(),
    )
    assert outcome["option_count"] == 2
    assert len(options) == 2
    assert not any("duplicate_family" in warning for warning in warnings)


def test_optioning_rejects_unrelated_refuting_experiment_as_falsification() -> None:
    research = _valid_option_research()
    hypothesis = research["root_cause_hypotheses"][0]
    attempt = hypothesis["falsification_attempts"][0]
    attempt["challenge_experiment_id"] = "exp-control"

    outcome, options, warnings = parse_optioning_response(
        json.dumps(
            {
                "problem_id": "problem:case",
                "optioning_status": "options_produced",
                "decision_rationale": "An unrelated green control was labeled refuting.",
                "options": [_valid_option()],
            }
        ),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct"},
        research_dossier=research,
    )

    assert outcome["option_count"] == 0
    assert options == []
    assert any("falsification_attempts_unverified" in warning for warning in warnings)


def test_optioning_rejects_legacy_bare_array_live_output() -> None:
    with pytest.raises(ValueError, match="legacy_bare_array_forbidden"):
        parse_optioning_response(
            json.dumps([_valid_option()]),
            expected_problem_id="problem:case",
            known_family_ids={"most_direct"},
        )


def test_shared_abstraction_requires_two_independent_evidenced_paths() -> None:
    option = _valid_option(
        mechanism="Introduce a shared normalization contract",
        scope_level="shared_abstraction",
    )
    option["summary"] = "Create a shared canonical result contract."
    payload = {
        "problem_id": "problem:case",
        "optioning_status": "options_produced",
        "decision_rationale": "A shared option was considered.",
        "options": [option],
    }

    outcome, options, warnings = parse_optioning_response(
        json.dumps(payload),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct", "most_robust", "most_comprehensive"},
    )

    assert outcome["optioning_status"] == "invalid_output"
    assert options == []
    assert any("requires_two_independent_paths" in warning for warning in warnings)


def test_shared_abstraction_requires_distinct_evidence_for_each_path() -> None:
    shared_ref = "src/shared.py:contract"
    option = _valid_option(
        mechanism="Introduce a shared normalization contract",
        scope_level="shared_abstraction",
        paths=[
            {"name": "consumer A", "evidence_refs": [shared_ref]},
            {"name": "consumer B", "evidence_refs": [shared_ref]},
        ],
    )
    option["summary"] = "Create a shared canonical result contract."
    outcome, options, warnings = parse_optioning_response(
        json.dumps(
            {
                "optioning_status": "options_produced",
                "decision_rationale": "Two named consumers were proposed.",
                "options": [option],
            }
        ),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct"},
        research_dossier={"inspected_symbols": [shared_ref]},
    )

    assert outcome["optioning_status"] == "invalid_output"
    assert options == []
    assert any("scope_path_receipt_unbound" in warning for warning in warnings)


def test_shared_abstraction_cannot_count_one_artifact_id_and_path_twice() -> None:
    option = _valid_option(
        mechanism="Introduce a shared normalization contract",
        scope_level="shared_abstraction",
        paths=[
            {"name": "consumer A", "evidence_refs": ["artifact:shared"]},
            {"name": "consumer B", "evidence_refs": ["evidence/shared.json"]},
        ],
    )
    option["summary"] = "Create a shared canonical result contract."
    outcome, options, warnings = parse_optioning_response(
        json.dumps(
            {
                "optioning_status": "options_produced",
                "decision_rationale": "Two aliases for one receipt were proposed.",
                "options": [option],
            }
        ),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct"},
        research_dossier={
            "artifact_refs": [
                {
                    "artifact_id": "artifact:shared",
                    "path": "evidence/shared.json",
                }
            ]
        },
    )

    assert outcome["optioning_status"] == "invalid_output"
    assert options == []
    assert any("scope_path_receipt_unbound" in warning for warning in warnings)


def test_option_scope_references_must_bind_to_research_evidence() -> None:
    option = _valid_option()
    payload = {
        "optioning_status": "options_produced",
        "decision_rationale": "One mechanism is supported.",
        "options": [option],
    }
    outcome, options, warnings = parse_optioning_response(
        json.dumps(payload),
        expected_problem_id="problem:case",
        known_family_ids={"most_direct"},
        research_dossier={"inspected_files": ["src/other.py"]},
    )
    assert outcome["optioning_status"] == "invalid_output"
    assert options == []
    assert any("scope_path_receipt_unbound" in warning for warning in warnings)


def test_selection_requires_causal_evaluation_and_matching_option() -> None:
    option = _valid_option()
    decision = {
        "problem_id": "problem:case",
        "selected_option_id": option["option_id"],
        "selected_family_id": option["family_id"],
        "causal_coverage_evaluation": {
            "mechanism_fit": "The traced boundary matches the mechanism.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": False,
        },
    }

    assert (
        selection_quality_errors(
            decision,
            expected_problem_id="problem:case",
            options_by_id={str(option["option_id"]): option},
        )
        == []
    )


def test_selection_cannot_accept_broad_option_without_class_level_evidence() -> None:
    option = _valid_option(
        scope_level="shared_abstraction",
        paths=[
            {"name": "consumer A", "evidence_refs": ["symbol:a"]},
            {"name": "consumer B", "evidence_refs": ["symbol:b"]},
        ],
    )
    decision = {
        "problem_id": "problem:case",
        "selected_option_id": option["option_id"],
        "selected_family_id": option["family_id"],
        "causal_coverage_evaluation": {
            "mechanism_fit": "The shared contract would cover both paths.",
            "accepted_unsupported_assumptions": [],
            "accepted_residual_risks": [],
            "class_level_evidence_sufficient": False,
        },
    }

    errors = selection_quality_errors(
        decision,
        expected_problem_id="problem:case",
        options_by_id={str(option["option_id"]): option},
        research_dossier={"inspected_symbols": ["symbol:a", "symbol:b"]},
        require_complete=True,
    )

    assert "selection_broad_scope_without_class_level_evidence" in errors


def test_falsification_review_can_explicitly_reject_without_being_malformed() -> None:
    research = _valid_option_research()
    control_id = _control_id(research)
    option = _valid_option()
    review = {
        "problem_id": "problem:case",
        "selected_option_id": "option:case:most_direct",
        "verdict": "reject",
        "strongest_counterargument": "The inspected caller bypasses the proposed boundary.",
        "evidence_refs": [
            {
                "ref": control_id,
                "finding": "The inspected caller bypasses the boundary",
                "effect": "challenges_selection",
            }
        ],
        "unsupported_assumptions": ["All callers use build_report"],
        "residual_risks": ["Alternate path remains unnormalized"],
        "critical_findings": [],
        "evidence_that_would_change_verdict": "Trace showing the alternate path is unreachable.",
        "material_risk_dispositions": [
            {
                "risk": "All callers use build_report",
                "disposition": "blocks_selection",
                "evidence_refs": [control_id],
                "rationale": "The alternate caller disproves this assumption.",
            },
            {
                "risk": "Alternate path remains unnormalized",
                "disposition": "blocks_selection",
                "evidence_refs": [control_id],
                "rationale": "The bypass leaves a recurrence path.",
            },
        ],
    }
    review = bind_falsification_review(
        review,
        problem_id="problem:case",
        selected_option=option,
        research=research,
    )

    assert (
        falsification_review_errors(
            review,
            expected_problem_id="problem:case",
            expected_option_id="option:case:most_direct",
            research_dossier=research,
            selected_option=option,
        )
        == []
    )


def test_falsification_must_dispose_every_material_risk_with_bound_evidence() -> None:
    research = _valid_option_research()
    control_id = _control_id(research)
    positive_contract_id = _add_positive_outcome_contract(research)
    option = _valid_option()
    coverage = option["causal_coverage"]
    assert isinstance(coverage, dict)
    coverage["compatibility_risks"] = ["Legacy caller can bypass normalization"]
    review = {
        "problem_id": "problem:case",
        "selected_option_id": "option:case:most_direct",
        "verdict": "accept",
        "strongest_counterargument": "The legacy caller bypasses the boundary.",
        "evidence_refs": [
            {
                "ref": control_id,
                "finding": "The legacy caller is covered by the proposed guard",
                "effect": "challenges_selection",
            }
        ],
        "unsupported_assumptions": [],
        "residual_risks": [],
        "critical_findings": [],
        "evidence_that_would_change_verdict": "A bypass trace after the guard.",
        "material_risk_dispositions": [],
        "selected_positive_outcome_contract_id": positive_contract_id,
        "outcome_contract_reviews": [
            {
                "positive_outcome_contract_id": positive_contract_id,
                "verdict": "sufficient",
                "semantic_relation_assessment": (
                    "The failed semantic test covers the report normalization behavior."
                ),
                "proves_intended_operation": True,
                "problem_coverage": "full",
                "residual_untested_paths": [],
                "evidence_refs": [control_id],
            }
        ],
    }
    review = bind_falsification_review(
        review,
        problem_id="problem:case",
        selected_option=option,
        research=research,
    )
    errors = falsification_review_errors(
        review,
        expected_problem_id="problem:case",
        expected_option_id="option:case:most_direct",
        research_dossier=research,
        selected_option=option,
    )
    assert any("undisposed_material_risks" in error for error in errors)

    review.pop("adversarial_evidence_receipt")
    review["material_risk_dispositions"] = [
        {
            "risk": "Legacy caller can bypass normalization",
            "disposition": "mitigated",
            "evidence_refs": [control_id],
            "rationale": "The same guard covers the legacy caller.",
        }
    ]
    review = bind_falsification_review(
        review,
        problem_id="problem:case",
        selected_option=option,
        research=research,
    )
    assert (
        falsification_review_errors(
            review,
            expected_problem_id="problem:case",
            expected_option_id="option:case:most_direct",
            research_dossier=research,
            selected_option=option,
        )
        == []
    )


def test_falsification_cannot_accept_with_only_supporting_self_attestation() -> None:
    review = {
        "problem_id": "problem:case",
        "selected_option_id": "option:case:most_direct",
        "verdict": "accept",
        "strongest_counterargument": "The alternate path might bypass normalization.",
        "evidence_refs": [
            {
                "ref": "exp-1",
                "finding": "The selected path reproduces the failure.",
                "effect": "supports_selection",
            }
        ],
        "unsupported_assumptions": [],
        "residual_risks": [],
        "critical_findings": [],
        "evidence_that_would_change_verdict": "A verified bypass trace.",
        "material_risk_dispositions": [],
    }

    errors = falsification_review_errors(
        review,
        expected_problem_id="problem:case",
        expected_option_id="option:case:most_direct",
        research_dossier={"experiments": [{"experiment_id": "exp-1"}]},
        selected_option=_valid_option(),
    )

    assert "falsification_accept_without_adversarial_evidence" in errors


def _valid_change_plan() -> dict[str, object]:
    plan = {
        "change_plan_id": "plan:case:1",
        "case_id": "case:case",
        "repo_revision": "abc123",
        "change_targets": [
            {
                "action": "modify",
                "path": "packages/runner_core/src/runner_core/runner.py",
                "symbols": ["runner.build_report"],
                "change": "Normalize the result before report assembly.",
            }
        ],
        "implementation_steps": [
            "Update `runner.py` at `build_report` to normalize the result before assembly."
        ],
        "verification_steps": ["Execute the focused lifecycle regression command."],
        "success_criteria": ["The original malformed-result fixture is classified."],
        "verification_commands": [
            "pdm -p packages/runner_core run pytest tests/test_lifecycle.py -q"
        ],
        "outcome_verification_roles": {
            "original_scenario": {
                "description": "Replay the original malformed result.",
                "research_experiment_id": "exp-1",
                "commands": [
                    "pdm -p packages/runner_core run pytest tests/test_lifecycle.py -q"
                ],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0}
                ],
            },
            "live": {
                "description": "Exercise the runtime report path.",
                "commands": ["python scripts/live_check.py"],
                "predicates": [
                    {"type": "command_exit_code", "command_index": 0, "equals": 0}
                ],
            },
            "mitigation_effect": None,
            "recurrence": None,
        },
        "before_after_reproduction": {
            "original_scenario": "Replay the malformed-result fixture.",
            "research_experiment_id": "exp-1",
            "expected_outcome_state": "resolved",
            "before_change": {
                "command": "pdm -p packages/runner_core run pytest tests/test_lifecycle.py -q",
                "expected_exit_code": 1,
                "expected_result": "The fixture fails with an unclassified result.",
                "observable_assertion": {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": 1,
                },
            },
            "after_change": {
                "command": "pdm -p packages/runner_core run pytest tests/test_lifecycle.py -q",
                "expected_exit_code": 0,
                "expected_result": "passes",
                "observable_assertions": [
                    {"source": "exit_code", "operator": "equals", "expected": 0}
                ],
            },
            "proof_limitation": None,
            "alternate_verification": None,
        },
        "compatibility_and_failure_modes": {
            "preserved_behaviors": ["Successful reports retain their current shape."],
            "intentional_changes": ["Malformed results receive a failure classification."],
            "failure_modes": ["Unknown result variants fail closed."],
            "migration_required": False,
        },
        "causal_coverage": {"mechanism_addressed": "Unnormalized runner result"},
        "scope_evidence": _valid_option()["scope_evidence"],
        "requires_live_verification": True,
        "live_verification_rationale": "The original failure occurred in a runtime pipeline.",
    }
    return assign_plan_revision_id(plan)


def test_change_plan_contract_accepts_decision_complete_plan() -> None:
    assert (
        change_plan_quality_errors(
            _valid_change_plan(),
            expected_revision="abc123",
            expected_case_id="case:case",
        )
        == []
    )


@pytest.mark.parametrize(
    "command",
    [
        "pytest tests/test_lifecycle.py || echo ok",
        "pytest tests/test_lifecycle.py; echo ok",
        "python -m pytest tests/test_lifecycle.py | Out-Null; exit 0",
        'bash -lc "pytest tests/test_lifecycle.py || echo ok"',
    ],
)
def test_change_plan_rejects_verification_commands_that_can_mask_failure(
    command: str,
) -> None:
    plan = _valid_change_plan()
    plan["verification_commands"] = [command]
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
    )

    assert any("change_plan_unsafe_verification_command" in error for error in errors)


def test_change_plan_revision_is_server_owned_and_content_addressed() -> None:
    plan = _valid_change_plan()
    original_revision = plan["plan_revision_id"]
    plan["plan_revision_id"] = "model-authored:v99"
    reassigned = assign_plan_revision_id(plan)
    assert reassigned["plan_revision_id"] == original_revision

    reassigned["proposed_fix"] = "A materially different fix"
    errors = change_plan_quality_errors(
        reassigned,
        expected_revision="abc123",
        expected_case_id="case:case",
    )
    assert any("invalid_plan_revision_id" in error for error in errors)


def test_change_plan_contract_rejects_discovery_first_steps() -> None:
    plan = _valid_change_plan()
    plan["implementation_steps"] = ["Locate the runner normalization path."]

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
    )

    assert any("discovery_first_step" in error for error in errors)


def test_planner_cannot_waive_runner_replay_with_prose_limitation() -> None:
    plan = _valid_change_plan()
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction.update(
        {
            "before_change": None,
            "after_change": None,
            "proof_limitation": "Live replay is unavailable.",
            "proof_limitation_refs": ["invented limitation"],
            "alternate_verification": "pytest -q",
        }
    )
    plan = assign_plan_revision_id(plan)
    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        research_dossier={"evidence_boundaries": ["No live service credentials"]},
    )
    assert any("unbound_proof_limitation" in error for error in errors)
    assert any("unverified_proof_limitation" in error for error in errors)
    assert any("alternate_not_in_verification_commands" in error for error in errors)

    reproduction["proof_limitation_refs"] = ["No live service credentials"]
    plan = assign_plan_revision_id(plan)
    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        research_dossier={"evidence_boundaries": ["No live service credentials"]},
    )
    assert any("unverified_proof_limitation" in error for error in errors)


def test_change_plan_create_target_requires_new_path_under_existing_parent(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    plan = _valid_change_plan()
    plan["change_targets"] = [
        {
            "action": "create",
            "path": "src/new_module.py",
            "symbols": ["NewModule"],
            "change": "Add the new isolated module.",
        }
    ]
    plan["verification_commands"] = ["pytest -q"]
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction["before_change"]["command"] = "pytest -q"
    reproduction["after_change"]["command"] = "pytest -q"
    plan = assign_plan_revision_id(plan)
    assert (
        change_plan_quality_errors(
            plan,
            expected_revision="abc123",
            expected_case_id="case:case",
            repo_root=tmp_path,
        )
        == []
    )

    (tmp_path / "src" / "new_module.py").write_text("class NewModule: pass\n")
    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        repo_root=tmp_path,
    )
    assert any("create_target_invalid" in error for error in errors)


def test_change_plan_qualified_symbol_cannot_match_unrelated_leaf(tmp_path: Path) -> None:
    source = tmp_path / "src" / "core.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class OtherClass:\n    def run(self):\n        return True\n",
        encoding="utf-8",
    )
    plan = _valid_change_plan()
    plan["change_targets"] = [
        {
            "action": "modify",
            "path": "src/core.py",
            "symbols": ["ExpectedClass.run"],
            "change": "Change the expected class method.",
        }
    ]
    plan["verification_commands"] = ["pytest -q"]
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction["before_change"]["command"] = "pytest -q"
    reproduction["after_change"]["command"] = "pytest -q"
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        repo_root=tmp_path,
    )

    assert any("target_symbol_missing" in error for error in errors)


def test_repo_grounding_requires_exact_clean_head(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "depth@example.test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Depth Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert assess_repo_grounding(tmp_path, revision)[0] is True
    tracked.write_text("dirty\n", encoding="utf-8")
    ready, reasons, _ = assess_repo_grounding(tmp_path, revision)
    assert ready is False
    assert "workspace_has_uncommitted_changes" in reasons
    tracked.write_text("one\n", encoding="utf-8")
    ready, reasons, _ = assess_repo_grounding(tmp_path, "0" * 40)
    assert ready is False
    assert any(reason.startswith("workspace_head_mismatch") for reason in reasons)


def test_optioning_uses_orchestrator_prompts_but_inspects_exact_target_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orchestrator_root = Path(__file__).resolve().parents[3]
    target_root = tmp_path / "target"
    target_root.mkdir()
    (target_root / "configs").mkdir()
    (target_root / "configs" / "repo_intent.md").write_text(
        "TARGET INTENT MUST NOT ENTER THE PROMPT\n", encoding="utf-8"
    )
    (target_root / "src.py").write_text("def run(): pass\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=target_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "depth@example.test"],
        cwd=target_root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Depth Test"], cwd=target_root, check=True)
    subprocess.run(["git", "add", "."], cwd=target_root, check=True)
    subprocess.run(["git", "commit", "-m", "target"], cwd=target_root, check=True)
    target_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    captured: dict[str, Any] = {}

    def _fake_stage_prompt(**kwargs: Any) -> str:
        captured.update(kwargs)
        return json.dumps(
            {
                "optioning_status": "options_produced",
                "decision_rationale": "The traced target path supports one mechanism.",
                "options": [
                    _valid_option(paths=[{"name": "target run path", "evidence_refs": ["exp-1"]}])
                ],
            }
        )

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.assess_research_readiness",
        lambda _dossier: (True, []),
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.verify_persisted_research_evidence",
        lambda _dossier: (True, []),
    )

    def _signed_projection(dossier: dict[str, Any]) -> dict[str, Any]:
        assert dossier["artifacts"]["untrusted_note"] == "UNTRUSTED_PROMPT_INJECTION"
        return {
            "problem_id": dossier["problem_id"],
            "repo_revision": dossier["repo_revision"],
            "signed_projection_marker": "SIGNED_RESEARCH_ONLY",
        }

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.research_prompt_projection",
        _signed_projection,
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.run_stage_prompt_json",
        _fake_stage_prompt,
    )
    manifest = load_pipeline_prompt_manifest(orchestrator_root / "configs" / "backlog_prompts")
    common_args: dict[str, Any] = {
        "repo_root": orchestrator_root,
        "atoms": [],
        "problem_records": [
            {
                "case_id": "case:case",
                "problem_id": "problem:case",
                "title": "Target failure",
                "problem": "Target path fails",
                "user_impact": "The target cannot complete",
                "evidence_atom_ids": [],
            }
        ],
        "priority_decisions": [
            {
                "case_id": "case:case",
                "problem_id": "problem:case",
                "selected_for_research": True,
            }
        ],
        "research_dossiers": [
            {
                "problem_id": "problem:case",
                "repo_revision": target_revision,
                "experiments": [{"experiment_id": "exp-1"}],
                "inspected_files": ["src.py"],
                "artifacts": {"untrusted_note": "UNTRUSTED_PROMPT_INJECTION"},
            }
        ],
        "pipeline_manifest": manifest,
        "artifacts_dir": tmp_path / "artifacts",
        "out_json": tmp_path / "options.json",
        "out_md": tmp_path / "options.md",
        "agent": "codex",
        "model": None,
        "cfg": object(),
        "dry_run": False,
        "breadth_profile": "default",
        "stage_guidance_text": "Test guidance",
    }
    _run_solution_optioning_stage(
        target_repo_roots_by_problem={"problem:case": target_root},
        **common_args,  # type: ignore[arg-type]
    )
    assert Path(captured["workspace_dir"]).resolve() == target_root.resolve()
    prompt = str(captured["prompt"])
    assert "SIGNED_RESEARCH_ONLY" in prompt
    assert "UNTRUSTED_PROMPT_INJECTION" not in prompt
    orchestrator_intent = (orchestrator_root / "configs" / "repo_intent.md").read_text(
        encoding="utf-8"
    )
    assert orchestrator_intent.strip() in prompt
    assert "TARGET INTENT MUST NOT ENTER THE PROMPT" not in prompt

    captured.clear()
    blocked = _run_solution_optioning_stage(
        target_repo_roots_by_problem=None,
        **common_args,  # type: ignore[arg-type]
    )
    assert captured == {}
    assert blocked["items"] == []
    metadata = blocked["input_meta"]
    assert metadata["target_workspace_count"] == 0
    assert metadata["optioning_outcomes"][0]["research_readiness_blockers"] == [
        "target_workspace_missing"
    ]

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.verify_persisted_research_evidence",
        lambda _dossier: (False, ["research_receipt_claims_changed"]),
    )
    captured.clear()
    invalid_receipt = _run_solution_optioning_stage(
        target_repo_roots_by_problem={"problem:case": target_root},
        **common_args,  # type: ignore[arg-type]
    )
    assert captured == {}
    assert invalid_receipt["items"] == []
    assert invalid_receipt["input_meta"]["optioning_outcomes"][0][
        "research_readiness_blockers"
    ] == ["persisted_research_evidence_invalid:research_receipt_claims_changed"]


def test_selection_and_planning_revalidate_persisted_research_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manifest = load_pipeline_prompt_manifest(repo_root / "configs" / "backlog_prompts")
    research = {"case_id": "case:case", "problem_id": "problem:case"}
    problem = {
        "case_id": "case:case",
        "problem_id": "problem:case",
        "title": "Target failure",
    }
    option = _valid_option()
    option["case_id"] = "case:case"

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_selection.assess_research_readiness",
        lambda _dossier: (True, []),
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_selection.verify_persisted_research_evidence",
        lambda _dossier: (False, ["research_artifact_changed:replay"]),
    )
    selection_doc = _run_solution_selection_stage(
        repo_root=repo_root,
        target_repo_roots_by_problem={},
        atoms=[],
        problem_records=[problem],
        research_dossiers=[research],
        solution_options=[option],
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "selection_artifacts",
        out_json=tmp_path / "selection.json",
        out_md=tmp_path / "selection.md",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        dry_run=False,
        breadth_profile="default",
        stage_guidance_text="Test guidance",
    )
    assert selection_doc["items"] == []
    assert selection_doc["input_meta"]["selection_outcomes"][0]["reasons"] == [
        "persisted_research_evidence_invalid:research_artifact_changed:replay"
    ]

    monkeypatch.setattr(
        "usertest_backlog.workflows.implementation_planning.assess_research_readiness",
        lambda _dossier: (True, []),
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.implementation_planning.verify_persisted_research_evidence",
        lambda _dossier: (False, ["research_artifact_changed:replay"]),
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.implementation_planning.assess_selection_readiness",
        lambda *_args, **_kwargs: (True, []),
    )
    planning_doc = _run_implementation_planning_stage(
        repo_root=repo_root,
        target_repo_roots_by_problem={},
        problem_records=[problem],
        research_dossiers=[research],
        solution_options=[option],
        selection_decisions=[
            {
                "case_id": "case:case",
                "problem_id": "problem:case",
                "selected_option_id": option["option_id"],
                "selected_option": option,
                "falsification_review": {"verdict": "accept"},
            }
        ],
        pipeline_manifest=manifest,
        artifacts_dir=tmp_path / "planning_artifacts",
        out_json=tmp_path / "plans.json",
        out_md=tmp_path / "plans.md",
        agent="codex",
        model=None,
        cfg=object(),  # type: ignore[arg-type]
        dry_run=False,
        stage_guidance_text="Test guidance",
    )
    assert planning_doc["items"] == []
    rejected = planning_doc["input_meta"]["rejected_plans"][0]
    assert any("persisted_research_evidence_invalid" in reason for reason in rejected["reasons"])
