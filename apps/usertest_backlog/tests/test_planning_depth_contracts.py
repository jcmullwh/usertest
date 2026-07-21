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
from backlog_miner.pipeline import (
    _write_model_invocation_manifest,
    load_pipeline_prompt_manifest,
)

import usertest_backlog.workflows.depth_contracts as depth_contracts
from usertest_backlog.workflows.depth_contracts import (
    _command_quality_errors,
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


def test_planner_symbol_grounding_accepts_utf8_bom_prefixed_python_binding() -> None:
    assert depth_contracts._target_symbol_exists(
        path=Path("scripts/batch_preflight.py"),
        content="\ufeffdef batch_preflight() -> None:\n    pass\n",
        symbol="batch_preflight.batch_preflight",
    )


@pytest.mark.parametrize(
    "content",
    [
        "def batch_preflight(:\n    pass\n",
        "\ufeffdef batch_preflight(:\n    pass\n",
    ],
)
def test_planner_symbol_grounding_rejects_python_syntax_errors(content: str) -> None:
    assert not depth_contracts._target_symbol_exists(
        path=Path("scripts/batch_preflight.py"),
        content=content,
        symbol="batch_preflight.batch_preflight",
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
            "outcome_strategy": {
                "intended_operation": (
                    "The original runner operation produces a classified report."
                ),
                "success_properties": [
                    "The original malformed-result replay reaches report assembly "
                    "as the expected classified result."
                ],
                "safety_constraints": [
                    "Already valid runner results retain their existing report semantics."
                ],
                "original_scenario_experiment_ids": ["exp-1"],
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
    consumer_projection = {
        "kind": "runner_observed_entrypoint",
        "entrypoint": "runner.build_report",
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
        "observed_polarity": {"polarity": "failure_persists_after_intervention"},
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
                "statement": ("The runner result reaches report assembly without normalization."),
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
                    "command": ("pytest tests/test_report.py::test_alternative_removed -q"),
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
    verified_mechanism = {
        "schema_version": 3,
        "mechanism_symbols": ["runner.build_report"],
        "code_paths": [
            {
                "symbol": "runner.build_report",
                "path": "packages/runner_core/src/runner_core/runner.py",
            }
        ],
    }
    verified_provenance = {
        "schema_version": 2,
        "primary_hypothesis_id": "h1",
        "mechanism_evidence_ids": [evidence_id],
        "causal_root_evidence_ids": [evidence_id],
    }
    mechanism_sha256 = _canonical_sha256(verified_mechanism)
    provenance_sha256 = _canonical_sha256(verified_provenance)
    verification["verified_mechanism"] = verified_mechanism
    verification["verified_mechanism_sha256"] = mechanism_sha256
    verification["verified_mechanism_provenance"] = verified_provenance
    verification["verified_mechanism_provenance_sha256"] = provenance_sha256
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "repository_test_assertion",
        "research_experiment_id": "exp-1",
        "mechanism_evidence_ids": [evidence_id],
        "primary_hypothesis_id": "h1",
        "primary_verified_mechanism_sha256": mechanism_sha256,
        "primary_verified_mechanism_provenance_sha256": provenance_sha256,
        "postconditions": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
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
        "primary_hypothesis_id": "h1",
        "primary_verified_mechanism_sha256": mechanism_sha256,
        "primary_verified_mechanism_provenance_sha256": provenance_sha256,
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
    oracle["outcome_oracle_id"] = _test_content_id("outcome_oracle", oracle, "outcome_oracle_id")
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


def test_selection_is_valid_without_family_telemetry() -> None:
    option = _valid_option()
    option.pop("family_id")
    decision = {
        "problem_id": "problem:case",
        "selected_option_id": option["option_id"],
        "causal_coverage_evaluation": {
            "mechanism_fit": "The traced boundary matches the established mechanism.",
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


def test_optioning_and_selection_prompts_treat_family_as_optional_telemetry() -> None:
    config_root = Path(__file__).resolve().parents[3] / "configs" / "backlog_prompts"
    optioner = (config_root / "solution_optioner.md").read_text(encoding="utf-8")
    selector = (config_root / "solution_selector.md").read_text(encoding="utf-8")

    assert "`family_id` is optional compatibility telemetry" in optioner
    assert "`selected_family_id` is optional" in selector
    assert "causal coverage, not a family label" in selector


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
        "outcome_strategy_review": {
            "verdict": "contradicted",
            "semantic_relation_assessment": (
                "The bypass means the proposed strategy does not cover every observed path."
            ),
            "proves_intended_operation": False,
            "problem_coverage": "partial",
            "residual_untested_paths": ["Alternate path remains unnormalized"],
            "evidence_refs": [control_id],
        },
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


def test_falsification_critical_finding_accepts_open_effect_description() -> None:
    research = _valid_option_research()
    control_id = _control_id(research)
    option = _valid_option()
    review = bind_falsification_review(
        {
            "problem_id": "problem:case",
            "selected_option_id": "option:case:most_direct",
            "verdict": "reject",
            "strongest_counterargument": "A provider-version boundary remains untested.",
            "evidence_refs": [
                {
                    "ref": control_id,
                    "finding": "The provider-version control exposes an untested boundary.",
                    "effect": "challenges_selection",
                }
            ],
            "unsupported_assumptions": [],
            "residual_risks": [],
            "critical_findings": [
                {
                    "finding": "The compatibility behavior differs by provider version.",
                    "affects": "cross-version provider compatibility and fallback behavior",
                    "evidence_refs": [control_id],
                }
            ],
            "evidence_that_would_change_verdict": "A controlled replay for both versions.",
            "material_risk_dispositions": [],
            "outcome_strategy_review": {
                "verdict": "insufficient_evidence",
                "semantic_relation_assessment": (
                    "The proposed result semantics are not established across provider versions."
                ),
                "proves_intended_operation": False,
                "problem_coverage": "unknown",
                "residual_untested_paths": [
                    "Cross-version provider compatibility remains untested"
                ],
                "evidence_refs": [control_id],
            },
        },
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

    assert not any("invalid_critical_finding" in error for error in errors)


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
        "outcome_strategy_review": {
            "verdict": "sufficient",
            "semantic_relation_assessment": (
                "The proposed strategy proves the intended normalized report behavior."
            ),
            "proves_intended_operation": True,
            "problem_coverage": "full",
            "residual_untested_paths": [],
            "evidence_refs": [control_id],
        },
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
                "commands": ["pdm -p packages/runner_core run pytest tests/test_lifecycle.py -q"],
                "predicates": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
            },
            "live": {
                "description": "Exercise the runtime report path.",
                "commands": ["python scripts/live_check.py"],
                "command_bindings": [
                    {"command_index": 0, "research_experiment_id": "exp-live"}
                ],
                "predicates": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
            },
            "mitigation_effect": None,
            "recurrence": {
                "description": "Use later canonical-case shadow snapshots.",
                "verification_owner": "centralized_case_refresh",
                "commands": [],
                "predicates": [],
            },
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


def test_change_plan_rejects_outcome_roles_that_ticket_export_cannot_serialize() -> None:
    plan = _valid_change_plan()
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    original["research_experiment_ids"] = ["exp-other", "exp-1"]
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
    )

    assert any(
        "change_plan_outcome_roles_export_contract_invalid" in error
        and "outcome_role_research_experiment_ids_invalid" in error
        for error in errors
    )


def test_change_plan_accepts_centrally_owned_recurrence_without_bespoke_probe() -> None:
    plan = _valid_change_plan()
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    roles["recurrence"] = {
        "description": "Use two later canonical-case shadow snapshots.",
        "verification_owner": "centralized_case_refresh",
        "commands": [],
        "predicates": [],
    }
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
    )

    assert not any("outcome_role_commands" in error for error in errors)
    assert not any("centralized_recurrence" in error for error in errors)


def test_change_plan_rejects_unowned_empty_recurrence_contract() -> None:
    plan = _valid_change_plan()
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    roles["recurrence"] = {
        "description": "Use later recurrence evidence.",
        "commands": [],
        "predicates": [],
    }
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
    )

    assert any("missing_outcome_role_commands" in error for error in errors)


def test_change_plan_keeps_bespoke_recurrence_probe_under_command_validation() -> None:
    plan = _valid_change_plan()
    roles = plan["outcome_verification_roles"]
    assert isinstance(roles, dict)
    roles["recurrence"] = {
        "description": "Run the verified problem-specific recurrence probe.",
        "commands": ["pytest tests/test_recurrence.py || echo ok"],
        "predicates": [{"type": "command_exit_code", "command_index": 0, "equals": 0}],
    }
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
    )

    assert any("unsafe_outcome_role_recurrence" in error for error in errors)


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


@pytest.mark.parametrize(
    "command",
    [
        "node --test",
        "deno test",
        "bun test",
        "ruby bin/rspec",
        "swift test",
        "vendor/bin/phpunit",
        "tools/verify-repository",
    ],
)
def test_verification_commands_accept_safe_repo_specific_tools(
    tmp_path: Path,
    command: str,
) -> None:
    for relative_path in ("bin/rspec", "vendor/bin/phpunit", "tools/verify-repository"):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("repository verification harness\n", encoding="utf-8")

    assert (
        _command_quality_errors(
            command,
            plan_id="plan:custom-tool",
            repo_root=tmp_path,
            label="verification_command",
        )
        == []
    )


def test_pytest_plugin_option_is_not_misread_as_project_path(tmp_path: Path) -> None:
    test_path = tmp_path / "tests" / "test_core.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_core(): pass\n", encoding="utf-8")

    assert (
        _command_quality_errors(
            "python -B -m pytest -p no:cacheprovider -q tests/test_core.py::test_core",
            plan_id="plan:pytest-plugin",
            repo_root=tmp_path,
            label="verification_command",
        )
        == []
    )


def test_wrapper_project_option_before_pytest_remains_project_bound(tmp_path: Path) -> None:
    project_root = tmp_path / "packages" / "core"
    test_path = project_root / "tests" / "test_core.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("def test_core(): pass\n", encoding="utf-8")

    assert (
        _command_quality_errors(
            "pdm -p packages/core run pytest -p no:cacheprovider -q tests/test_core.py",
            plan_id="plan:wrapped-pytest",
            repo_root=tmp_path,
            label="verification_command",
        )
        == []
    )
    assert any(
        "project_path_missing" in error
        for error in _command_quality_errors(
            "pdm -p packages/missing run pytest -p no:cacheprovider -q tests/test_core.py",
            plan_id="plan:missing-wrapper-project",
            repo_root=tmp_path,
            label="verification_command",
        )
    )


def test_verification_command_can_bind_an_unlisted_runner_to_a_planned_create_target(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    command = "dotnet test tests/New.Tests.csproj"

    assert (
        _command_quality_errors(
            command,
            plan_id="plan:planned-test-project",
            repo_root=tmp_path,
            label="verification_command",
            planned_create_paths={"tests/New.Tests.csproj"},
        )
        == []
    )
    assert any(
        "command_path_missing" in error
        for error in _command_quality_errors(
            command,
            plan_id="plan:unbound-test-project",
            repo_root=tmp_path,
            label="verification_command",
        )
    )


def test_retained_replay_path_requires_exact_bound_command_and_asset_path(
    tmp_path: Path,
) -> None:
    command = (
        "python -B -m pytest -p no:cacheprovider -q -s "
        ".usertest_research/test_retained_replay.py::test_fail_first"
    )
    retained_path = ".usertest_research/test_retained_replay.py"

    assert (
        _command_quality_errors(
            command,
            plan_id="plan:retained-replay",
            repo_root=tmp_path,
            label="verification_command",
            bound_asset_paths={retained_path},
        )
        == []
    )
    assert any(
        "command_path_missing" in error
        for error in _command_quality_errors(
            command,
            plan_id="plan:unbound-retained-replay",
            repo_root=tmp_path,
            label="verification_command",
        )
    )
    sibling_command = command.replace("test_retained_replay.py", "forged_replay.py")
    assert any(
        "command_path_missing" in error
        for error in _command_quality_errors(
            sibling_command,
            plan_id="plan:wrong-retained-path",
            repo_root=tmp_path,
            label="verification_command",
            bound_asset_paths={retained_path},
        )
    )


def test_change_plan_path_resolution_is_scoped_to_bound_replay_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = (
        "python -B -m pytest -p no:cacheprovider -q -s "
        ".usertest_research/test_retained_replay.py::test_fail_first"
    )
    retained_path = ".usertest_research/test_retained_replay.py"
    normalized_command = " ".join(command.split())
    monkeypatch.setattr(
        depth_contracts,
        "verified_staged_replay_command_asset_paths",
        lambda _plan, *, research: {normalized_command: {retained_path}},
    )
    plan = _valid_change_plan()
    plan["verification_commands"] = [command]
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction["before_change"]["command"] = command
    reproduction["after_change"]["command"] = command
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        repo_root=tmp_path,
    )
    assert not any("path_missing" in error and ".usertest_research" in error for error in errors)

    sibling_command = command.replace("test_retained_replay.py", "forged_replay.py")
    reproduction["after_change"]["command"] = sibling_command
    plan = assign_plan_revision_id(plan)
    sibling_errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        repo_root=tmp_path,
    )
    assert any(
        "change_plan_after_command_path_missing" in error and "forged_replay.py" in error
        for error in sibling_errors
    )


def test_historical_overlay_path_is_allowed_only_in_before_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = "python -B .usertest_research/historical_probe.py"
    normalized_command = " ".join(command.split())
    retained_path = ".usertest_research/historical_probe.py"
    monkeypatch.setattr(
        depth_contracts,
        "verified_research_overlay_command_asset_paths",
        lambda _research, *, experiment_id: (
            {normalized_command: {retained_path}}
            if experiment_id == "experiment:original"
            else {}
        ),
    )
    plan = _valid_change_plan()
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction["research_experiment_id"] = "experiment:original"
    reproduction["before_change"]["command"] = command
    plan = assign_plan_revision_id(plan)

    before_errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        repo_root=tmp_path,
    )

    assert not any(
        "change_plan_before_command_path_missing" in error
        and retained_path in error
        for error in before_errors
    )

    reproduction["after_change"]["command"] = command
    plan["verification_commands"] = [command]
    plan = assign_plan_revision_id(plan)
    after_errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        repo_root=tmp_path,
    )

    assert any(
        "path_missing" in error and retained_path in error for error in after_errors
    )


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


def test_proof_limitation_must_be_bound_and_use_an_executable_alternate() -> None:
    plan = _valid_change_plan()
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    reproduction.update(
        {
            "before_change": None,
            "after_change": None,
            "proof_limitation": "Live replay is unavailable.",
            "proof_limitation_refs": ["invented limitation"],
            "alternate_verification": plan["verification_commands"][0],
            "expected_outcome_state": "unverified",
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
    assert not any("unverified_proof_limitation" in error for error in errors)

    reproduction["proof_limitation_refs"] = ["No live service credentials"]
    plan = assign_plan_revision_id(plan)
    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        research_dossier={"evidence_boundaries": ["No live service credentials"]},
    )
    assert not any("proof_limitation" in error for error in errors)
    assert not any("limited_outcome" in error for error in errors)


def test_material_change_surface_limitation_returns_plan_to_research() -> None:
    plan = _valid_change_plan()
    reproduction = plan["before_after_reproduction"]
    assert isinstance(reproduction, dict)
    alternate = plan["verification_commands"][0]
    reproduction.update(
        {
            "before_change": None,
            "after_change": None,
            "proof_limitation": "The controlling boundary is not yet established.",
            "proof_limitation_refs": ["unknown:control-boundary"],
            "alternate_verification": alternate,
            "expected_outcome_state": "unverified",
        }
    )
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        research_dossier={
            "material_unknowns": [
                {
                    "unknown_id": "unknown:control-boundary",
                    "unknown": "The controlling boundary is not yet established.",
                    "affects": ["change_surface"],
                }
            ]
        },
    )

    assert any("material_limitation_requires_research" in error for error in errors)


@pytest.mark.parametrize(
    ("target", "source_paths"),
    [
        (
            {
                "action": "modify",
                "path": "config/runtime.json",
                "change": "Change the selected provider value.",
            },
            ["config/runtime.json"],
        ),
        (
            {
                "action": "delete",
                "path": "assets/obsolete.json",
                "change": "Remove the superseded asset.",
            },
            ["assets/obsolete.json"],
        ),
        (
            {
                "action": "rename",
                "path": "schemas/old.json",
                "destination_path": "schemas/current.json",
                "change": "Rename the schema while preserving its contents.",
            },
            ["schemas/old.json"],
        ),
        (
            {
                "action": "move",
                "path": "assets/schema.json",
                "destination_path": "schemas/schema.json",
                "change": "Move the schema to the runtime-consumed location.",
            },
            ["assets/schema.json"],
        ),
        (
            {
                "action": "create",
                "path": "schemas/new.json",
                "change": "Create the selected schema contract.",
            },
            [],
        ),
    ],
)
def test_change_targets_support_file_level_and_relocation_work(
    tmp_path: Path,
    target: dict[str, object],
    source_paths: list[str],
) -> None:
    for relative_path in source_paths:
        source = tmp_path / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("{}\n", encoding="utf-8")
    (tmp_path / "schemas").mkdir(exist_ok=True)
    plan = _valid_change_plan()
    plan["change_targets"] = [target]
    plan = assign_plan_revision_id(plan)

    errors = change_plan_quality_errors(
        plan,
        expected_revision="abc123",
        expected_case_id="case:case",
        repo_root=tmp_path,
    )

    assert not any("target_action" in error for error in errors)
    assert not any("target_symbols" in error for error in errors)
    assert not any("target_destination" in error for error in errors)
    assert not any("target_path_missing" in error for error in errors)


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


def test_repo_grounding_uses_command_scoped_safe_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "retained-worktree"
    revision = "a" * 40
    calls: list[list[str]] = []

    def _fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stdout = f"{revision}\n" if command[-2:] == ["rev-parse", "HEAD"] else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(depth_contracts.subprocess, "run", _fake_run)

    assert depth_contracts.read_repo_revision(repo_root) == revision
    assert depth_contracts.repo_contains_revision(repo_root, revision) is True
    ready, reasons, context = assess_repo_grounding(repo_root, revision)

    assert ready is True
    assert reasons == []
    assert context["clean"] is True
    safe_argument = f"safe.directory={repo_root.resolve()}"
    assert len(calls) == 4
    assert all(command[:3] == ["git", "-c", safe_argument] for command in calls)


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
        response = json.dumps(
            {
                "optioning_status": "options_produced",
                "decision_rationale": "The traced target path supports one mechanism.",
                "options": [
                    _valid_option(paths=[{"name": "target run path", "evidence_refs": ["exp-1"]}])
                ],
            }
        )
        out_dir = Path(kwargs["out_dir"])
        tag = str(kwargs["tag"])
        prompt = str(kwargs["prompt"])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{tag}.prompt.txt").write_text(
            prompt,
            encoding="utf-8",
            newline="\n",
        )
        (out_dir / f"{tag}.response.txt").write_text(
            response,
            encoding="utf-8",
            newline="\n",
        )
        (out_dir / f"{tag}.raw_events.jsonl").write_text(
            "{}\n",
            encoding="utf-8",
            newline="\n",
        )
        (out_dir / f"{tag}.last_message.txt").write_text(
            response,
            encoding="utf-8",
            newline="\n",
        )
        (out_dir / f"{tag}.stderr.txt").write_text(
            "",
            encoding="utf-8",
            newline="\n",
        )
        _write_model_invocation_manifest(
            stage=str(kwargs["stage"]),
            tag=tag,
            agent=str(kwargs["agent"]),
            out_dir=out_dir,
            prompt=prompt,
            response=response,
            error_kind=None,
        )
        return response

    def _accept_research(dossier: dict[str, Any]) -> tuple[bool, list[str]]:
        assert "canonical_problem_id" not in dossier
        assert "case_member_problem_ids" not in dossier
        return True, []

    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.assess_research_readiness",
        _accept_research,
    )
    monkeypatch.setattr(
        "usertest_backlog.workflows.solution_options.verify_persisted_research_evidence",
        _accept_research,
    )

    def _signed_projection(dossier: dict[str, Any]) -> dict[str, Any]:
        assert dossier["artifacts"]["untrusted_note"] == "UNTRUSTED_PROMPT_INJECTION"
        assert "canonical_problem_id" not in dossier
        assert "case_member_problem_ids" not in dossier
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
                "canonical_problem_id": "problem:case",
                "case_member_problem_ids": ["problem:case", "problem:symptom"],
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
        "agent": "claude",
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

    for disposition in ("already_addressed", "non_actionable"):
        common_args["research_dossiers"][0]["actionability_assessment"] = {
            "disposition": disposition,
            "rationale": "The retained Stage 3 proof establishes that no change is required.",
            "evidence_refs": ["exp-1"],
        }
        captured.clear()
        terminal = _run_solution_optioning_stage(
            target_repo_roots_by_problem=None,
            **common_args,  # type: ignore[arg-type]
        )
        assert captured == {}
        assert terminal["items"] == []
        terminal_outcome = terminal["input_meta"]["optioning_outcomes"][0]
        assert terminal_outcome["optioning_status"] == "not_required"
        assert terminal_outcome["research_actionability_disposition"] == disposition
        assert terminal_outcome["evidence_refs"] == ["exp-1"]
    common_args["research_dossiers"][0].pop("actionability_assessment")

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


def test_research_contract_view_removes_only_server_owned_lineage_annotations() -> None:
    from usertest_backlog.workflows.depth_contracts import research_contract_view

    persisted = {
        "problem_id": "problem:case",
        "canonical_problem_id": "problem:case",
        "case_member_problem_ids": ["problem:case", "problem:symptom"],
        "repo_revision": "a" * 40,
        "unknown_authored_field": "must remain visible to strict validation",
    }

    contract = research_contract_view(persisted)

    assert "canonical_problem_id" not in contract
    assert "case_member_problem_ids" not in contract
    assert contract["problem_id"] == "problem:case"
    assert contract["unknown_authored_field"] == "must remain visible to strict validation"
    assert persisted["canonical_problem_id"] == "problem:case"
