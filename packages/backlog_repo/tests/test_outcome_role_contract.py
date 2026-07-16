from __future__ import annotations

import hashlib
import json

import pytest

from backlog_repo.ticket_provenance import (
    parse_verification_contract_markdown,
    render_verification_contract_markdown,
)


def _roles() -> dict[str, object]:
    return {
        "original_scenario": {
            "description": "Replay the exact original scenario.",
            "research_experiment_id": "experiment:original",
            "commands": ["python original_probe.py"],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0}
            ],
        },
        "live": {
            "description": "Probe the deployed runtime.",
            "commands": ["dotnet test tests/Runtime.Tests.csproj --filter LiveProbe"],
            "command_bindings": [
                {
                    "command_index": 0,
                    "research_experiment_id": "experiment:live",
                }
            ],
            "predicates": [
                {"type": "command_exit_code", "command_index": 0, "equals": 0},
                {
                    "type": "command_stdout_contains",
                    "command_index": 0,
                    "value": "healthy",
                },
            ],
        },
        "mitigation_effect": None,
        "recurrence": {
            "description": "Use two later canonical-case shadow snapshots.",
            "verification_owner": "centralized_case_refresh",
            "commands": [],
            "predicates": [],
        },
    }


def _stage5_planned_roles() -> dict[str, object]:
    strategy = {
        "intended_operation": "The original operation completes with its useful result.",
        "success_properties": ["The retained replay emits useful-result=ready."],
        "safety_constraints": ["Existing successful inputs remain unchanged."],
        "original_scenario_experiment_ids": ["experiment:original"],
    }
    review = {
        "verdict": "sufficient",
        "semantic_relation_assessment": "The property proves useful operation, not marker loss.",
        "proves_intended_operation": True,
        "problem_coverage": "full",
        "residual_untested_paths": [],
        "evidence_refs": ["mechanism_evidence:original"],
    }
    selected: dict[str, object] = {
        "schema_version": 1,
        "producer": "backlog_core.bind_falsification_review",
        "kind": "selected_option_outcome_strategy",
        "problem_id": "problem:original",
        "selected_option_id": "option:original",
        "selected_option_sha256": "a" * 64,
        "research_receipt_sha256": "b" * 64,
        "strategy": strategy,
        "review": review,
        "outcome_contract_status": "approved_for_planning",
        "post_change_evidence_status": "unverified",
        "maximum_post_change_outcome_claim": "resolved",
        "outcome_claim_status": "resolved",
        "outcome_confidence": "full",
        "research_baseline_positive_outcome_contract_ids": [
            "positive_outcome_contract:baseline"
        ],
        "research_baseline_reviews_sha256": "c" * 64,
    }
    selected["outcome_contract_id"] = "stage5_outcome_contract:" + _sha256_json(selected)
    oracle: dict[str, object] = {
        "schema_version": 1,
        "kind": "stage5_planned_outcome",
        "proof_scope": "planned_post_change_verification",
        "case_id": "case:original",
        "repo_revision": "d" * 40,
        "research_experiment_id": "experiment:original",
        "research_experiment_ids": ["experiment:original"],
        "selected_outcome_contract": selected,
    }
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _sha256_json(oracle)
    roles = _roles()
    roles["original_scenario"] = {
        "description": "Execute Stage 6's exact post-change replay.",
        "research_experiment_id": "experiment:original",
        "research_experiment_ids": ["experiment:original"],
        "commands": ["python original_probe.py"],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "useful-result=ready",
            },
        ],
        "oracle": oracle,
        "required_proof_scope": "planned_post_change_verification",
    }
    return roles


def _fail_first_stage5_roles() -> dict[str, object]:
    roles = _stage5_planned_roles()
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    planned_oracle = original["oracle"]
    assert isinstance(planned_oracle, dict)
    selected = planned_oracle["selected_outcome_contract"]
    assert isinstance(selected, dict)
    argv = ["python", ".usertest_research/fail_first.py"]
    oracle: dict[str, object] = {
        "schema_version": 1,
        "case_id": "case:original",
        "repo_revision": planned_oracle["repo_revision"],
        "research_experiment_id": "experiment:original",
        "scenario_kind": "fail_first_contract",
        "origin_atom_ids": ["atom:original"],
        "mechanism_evidence_ids": ["mechanism_evidence:original"],
        "baseline": {
            "exit_code": 1,
            "observable_assertion": {
                "source": "stderr",
                "operator": "contains",
                "expected": "original failure",
            },
            "stdout_sha256": "b" * 64,
            "stderr_sha256": "c" * 64,
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": argv,
            "command_authorization": {
                "authorization_kind": "attested_research_harness",
                "executed_argv_sha256": _sha256_json(argv),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
        "selected_outcome_contract": selected,
    }
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _sha256_json(oracle)
    roles["original_scenario"] = {
        "description": "Replay the unchanged fail-first contract after implementation.",
        "research_experiment_id": "experiment:original",
        "research_experiment_ids": ["experiment:original"],
        "commands": [],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "useful-result=ready",
            },
        ],
        "oracle": oracle,
        "required_proof_scope": "behavioral",
    }
    contract_id = selected["outcome_contract_id"]
    repo_revision = oracle["repo_revision"]
    roles["live"] = {
        "description": "Run the planned live verification after implementation.",
        "execution_status": "planned_unverified",
        "commands": ["python scripts/live_capacity_recovery.py --verify"],
        "command_bindings": [
            {
                "command_index": 0,
                "binding_kind": "stage6_planned_post_change",
                "selected_outcome_contract_id": contract_id,
                "repo_revision": repo_revision,
            }
        ],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    roles["mitigation_effect"] = {
        "description": "Run the planned mitigation verification after implementation.",
        "execution_status": "planned_unverified",
        "commands": ["python scripts/mitigation_capacity_recovery.py --verify"],
        "command_bindings": [
            {
                "command_index": 0,
                "binding_kind": "stage6_planned_post_change",
                "selected_outcome_contract_id": contract_id,
                "repo_revision": repo_revision,
            }
        ],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    return roles


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _origin_positive_contract(
    *,
    experiment_id: str,
    atom_id: str,
) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": experiment_id,
        "mechanism_evidence_ids": [f"mechanism_evidence:{experiment_id}"],
        "origin_evidence": {
            "atom_id": atom_id,
            "atom_sha256": _sha256_json({"atom_id": atom_id}),
            "field_path": "$.expected_output",
            "value_sha256": _sha256_json({"expected_output": experiment_id}),
        },
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    contract["positive_outcome_contract_id"] = (
        "positive_outcome_contract:" + _sha256_json(contract)
    )
    return contract


def _staged_replay_oracle(
    *,
    case_id: str,
    experiment_id: str,
    atom_id: str,
    contract: dict[str, object],
) -> dict[str, object]:
    argv = ["python", f".usertest_research/{experiment_id}.py"]
    oracle: dict[str, object] = {
        "schema_version": 1,
        "case_id": case_id,
        "repo_revision": "a" * 40,
        "research_experiment_id": experiment_id,
        "scenario_kind": "original_replay",
        "origin_atom_ids": [atom_id],
        "mechanism_evidence_ids": [f"mechanism_evidence:{experiment_id}"],
        "baseline": {
            "exit_code": 1,
            "observable_assertion": {
                "source": "stderr",
                "operator": "contains",
                "expected": f"failure:{experiment_id}",
            },
            "stdout_sha256": "b" * 64,
            "stderr_sha256": "c" * 64,
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": argv,
            "command_authorization": {
                "authorization_kind": "standard_test_or_research_harness",
                "executed_argv_sha256": _sha256_json(argv),
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
        "positive_outcome_contracts": [contract],
    }
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _sha256_json(oracle)
    return oracle


def _multi_scenario_roles() -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    experiment_ids = ["experiment:first", "experiment:second"]
    contracts = [
        _origin_positive_contract(
            experiment_id=experiment_id,
            atom_id=f"atom:{index}",
        )
        for index, experiment_id in enumerate(experiment_ids, start=1)
    ]
    child_oracles = [
        _staged_replay_oracle(
            case_id=f"case:{index}",
            experiment_id=experiment_id,
            atom_id=f"atom:{index}",
            contract=contract,
        )
        for index, (experiment_id, contract) in enumerate(
            zip(experiment_ids, contracts, strict=True),
            start=1,
        )
    ]
    scenarios: list[dict[str, object]] = []
    for oracle, contract in zip(child_oracles, contracts, strict=True):
        scenario: dict[str, object] = {
            "positive_outcome_contract_id": contract[
                "positive_outcome_contract_id"
            ],
            "oracle": oracle,
            "predicates": contract["postconditions"],
            "after_change": {},
        }
        scenario["scenario_id"] = "outcome_scenario:" + _sha256_json(scenario)
        scenarios.append(scenario)

    outer_oracle: dict[str, object] = {
        "schema_version": 1,
        "kind": "multi_scenario",
        "case_id": "case:canonical",
        "repo_revision": "a" * 40,
        "proof_scope": "multi_scenario",
        "positive_outcome_contracts": contracts,
        "scenarios": scenarios,
    }
    outer_oracle["outcome_oracle_id"] = (
        "outcome_oracle:" + _sha256_json(outer_oracle)
    )
    roles = _roles()
    roles["original_scenario"] = {
        "description": "Replay every retained original scenario.",
        "research_experiment_id": experiment_ids[0],
        "research_experiment_ids": experiment_ids,
        "selected_positive_outcome_contract_ids": [
            contract["positive_outcome_contract_id"] for contract in contracts
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
        "oracle": outer_oracle,
        "required_proof_scope": "multi_scenario",
    }
    return roles, scenarios, contracts


def test_v2_contract_round_trip_retains_role_hashes() -> None:
    markdown = render_verification_contract_markdown(
        ["python -m pytest tests/test_feature.py -q"],
        outcome_roles=_roles(),
    )

    parsed = parse_verification_contract_markdown(markdown)

    assert parsed is not None
    assert parsed["schema_version"] == 2
    assert parsed["outcome_roles"]["original_scenario"][
        "research_experiment_id"
    ] == "experiment:original"
    assert len(
        parsed["outcome_roles"]["original_scenario"]["role_contract_sha256"]
    ) == 64
    assert parsed["outcome_roles"]["recurrence"]["commands"] == []
    assert (
        parsed["outcome_roles"]["recurrence"]["verification_owner"]
        == "centralized_case_refresh"
    )


@pytest.mark.parametrize(
    ("source", "operator"),
    [
        (source, operator)
        for source in ("stdout", "stderr", "combined")
        for operator in ("contains", "not_contains", "equals")
    ],
)
def test_command_stream_predicate_vocabulary_round_trips(
    source: str,
    operator: str,
) -> None:
    roles = _roles()
    live = roles["live"]
    assert isinstance(live, dict)
    predicate = {
        "type": f"command_{source}_{operator}",
        "command_index": 0,
        "value": "  expected observation  ",
    }
    live["predicates"] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0},
        predicate,
    ]

    markdown = render_verification_contract_markdown(
        ["python -m pytest tests/test_feature.py -q"],
        outcome_roles=roles,
    )
    parsed = parse_verification_contract_markdown(markdown)

    assert parsed is not None
    assert parsed["outcome_roles"]["live"]["predicates"][1] == {
        **predicate,
        "value": "expected observation",
    }


def test_stage5_planned_outcome_round_trip_keeps_exact_stage6_checks() -> None:
    roles = _stage5_planned_roles()

    markdown = render_verification_contract_markdown(
        ["python original_probe.py"],
        outcome_roles=roles,
    )
    parsed = parse_verification_contract_markdown(markdown)

    assert parsed is not None
    original = parsed["outcome_roles"]["original_scenario"]
    assert original["oracle"]["kind"] == "stage5_planned_outcome"
    assert original["commands"] == ["python original_probe.py"]
    assert original["oracle"]["selected_outcome_contract"][
        "post_change_evidence_status"
    ] == "unverified"


def test_fail_first_plan_export_uses_stage5_contract_without_inventing_positive_contract() -> None:
    roles = _fail_first_stage5_roles()

    markdown = render_verification_contract_markdown(
        ["python -m pytest tests/test_feature.py -q"],
        outcome_roles=roles,
    )
    parsed = parse_verification_contract_markdown(markdown)

    assert parsed is not None
    original = parsed["outcome_roles"]["original_scenario"]
    oracle = original["oracle"]
    selected_id = oracle["selected_outcome_contract"]["outcome_contract_id"]
    assert oracle["kind"] == "staged_replay"
    assert oracle["scenario_kind"] == "fail_first_contract"
    assert "positive_outcome_contracts" not in oracle
    assert "selected_positive_outcome_contract_ids" not in original
    assert parsed["outcome_roles"]["live"]["command_bindings"][0][
        "selected_outcome_contract_id"
    ] == selected_id
    assert parsed["outcome_roles"]["mitigation_effect"]["command_bindings"][0][
        "selected_outcome_contract_id"
    ] == selected_id


def test_fail_first_plan_export_rejects_missing_stage5_contract() -> None:
    roles = _fail_first_stage5_roles()
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    oracle = original["oracle"]
    assert isinstance(oracle, dict)
    oracle.pop("selected_outcome_contract")
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _sha256_json(
        {key: value for key, value in oracle.items() if key != "outcome_oracle_id"}
    )

    with pytest.raises(ValueError, match="fail_first_planned_oracle_invalid"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )


def test_ordinary_staged_replay_still_requires_unambiguous_positive_selection() -> None:
    first = _origin_positive_contract(
        experiment_id="experiment:original",
        atom_id="atom:original",
    )
    second = _origin_positive_contract(
        experiment_id="experiment:alternate",
        atom_id="atom:alternate",
    )
    oracle = _staged_replay_oracle(
        case_id="case:original",
        experiment_id="experiment:original",
        atom_id="atom:original",
        contract=first,
    )
    oracle["positive_outcome_contracts"] = [first, second]
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _sha256_json(
        {key: value for key, value in oracle.items() if key != "outcome_oracle_id"}
    )
    roles = _roles()
    roles["original_scenario"] = {
        "description": "Replay an ordinary staged oracle.",
        "research_experiment_id": "experiment:original",
        "commands": [],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
        "oracle": oracle,
        "required_proof_scope": "behavioral",
    }

    with pytest.raises(ValueError, match="selected_positive_contract_ids_invalid"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )


def test_stage5_planned_live_command_round_trip_preserves_unverified_plan_binding() -> None:
    roles = _stage5_planned_roles()
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    oracle = original["oracle"]
    assert isinstance(oracle, dict)
    selected = oracle["selected_outcome_contract"]
    assert isinstance(selected, dict)
    contract_id = selected["outcome_contract_id"]
    repo_revision = oracle["repo_revision"]
    roles["live"] = {
        "description": "Run the selected post-change capacity recovery.",
        "execution_status": "planned_unverified",
        "commands": ["python scripts/live_capacity_recovery.py --verify"],
        "command_bindings": [
            {
                "command_index": 0,
                "binding_kind": "stage6_planned_post_change",
                "selected_outcome_contract_id": contract_id,
                "repo_revision": repo_revision,
            }
        ],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0},
            {
                "type": "command_stdout_contains",
                "command_index": 0,
                "value": "capacity_recovered=true",
            },
        ],
    }

    markdown = render_verification_contract_markdown(
        ["python original_probe.py"],
        outcome_roles=roles,
    )
    parsed = parse_verification_contract_markdown(markdown)

    assert parsed is not None
    live = parsed["outcome_roles"]["live"]
    assert live["execution_status"] == "planned_unverified"
    assert live["command_bindings"] == [
        {
            "command_index": 0,
            "binding_kind": "stage6_planned_post_change",
            "selected_outcome_contract_id": contract_id,
            "repo_revision": repo_revision,
        }
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("selected_outcome_contract_id", "stage5_outcome_contract:wrong"),
        ("repo_revision", "e" * 40),
    ],
)
def test_stage5_planned_live_binding_must_match_original_oracle(
    field: str,
    value: str,
) -> None:
    roles = _stage5_planned_roles()
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    oracle = original["oracle"]
    assert isinstance(oracle, dict)
    selected = oracle["selected_outcome_contract"]
    assert isinstance(selected, dict)
    binding = {
        "command_index": 0,
        "binding_kind": "stage6_planned_post_change",
        "selected_outcome_contract_id": selected["outcome_contract_id"],
        "repo_revision": oracle["repo_revision"],
    }
    binding[field] = value
    roles["live"] = {
        "description": "Run the selected post-change capacity recovery.",
        "execution_status": "planned_unverified",
        "commands": ["python scripts/live_capacity_recovery.py --verify"],
        "command_bindings": [binding],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }

    with pytest.raises(
        ValueError,
        match="outcome_role_planned_command_binding_invalid:live:0",
    ):
        render_verification_contract_markdown(
            ["python original_probe.py"],
            outcome_roles=roles,
        )


def test_stage5_planned_outcome_rejects_false_preimplementation_resolution() -> None:
    roles = _stage5_planned_roles()
    original = roles["original_scenario"]
    assert isinstance(original, dict)
    oracle = original["oracle"]
    assert isinstance(oracle, dict)
    selected = oracle["selected_outcome_contract"]
    assert isinstance(selected, dict)
    selected["post_change_evidence_status"] = "resolved"
    selected["outcome_contract_id"] = "stage5_outcome_contract:" + _sha256_json(
        {key: value for key, value in selected.items() if key != "outcome_contract_id"}
    )
    oracle["outcome_oracle_id"] = "outcome_oracle:" + _sha256_json(
        {key: value for key, value in oracle.items() if key != "outcome_oracle_id"}
    )

    with pytest.raises(ValueError, match="outcome_role_stage5_planned_oracle_invalid"):
        render_verification_contract_markdown(
            ["python original_probe.py"],
            outcome_roles=roles,
        )


def test_unknown_command_stream_predicate_is_rejected() -> None:
    roles = _roles()
    live = roles["live"]
    assert isinstance(live, dict)
    live["predicates"] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0},
        {
            "type": "command_stdout_regex",
            "command_index": 0,
            "value": "expected.*observation",
        },
    ]

    with pytest.raises(ValueError, match="outcome_role_predicate_type_invalid"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )


def test_live_and_recurrence_roles_cannot_relabel_generic_tests() -> None:
    roles = _roles()
    recurrence = roles["recurrence"]
    assert isinstance(recurrence, dict)
    recurrence.pop("verification_owner")
    recurrence["commands"] = ["python -m pytest tests/test_feature.py -q"]
    recurrence["command_bindings"] = [
        {
            "command_index": 0,
            "research_experiment_id": "experiment:recurrence",
        }
    ]
    recurrence["predicates"] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0}
    ]

    with pytest.raises(ValueError, match="generic_command_reused"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )


def test_centralized_recurrence_requires_explicit_owner_marker() -> None:
    roles = _roles()
    recurrence = roles["recurrence"]
    assert isinstance(recurrence, dict)
    recurrence.pop("verification_owner")

    with pytest.raises(ValueError, match="verification_contract_commands_empty"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )


def test_centralized_recurrence_cannot_smuggle_plan_owned_work() -> None:
    roles = _roles()
    recurrence = roles["recurrence"]
    assert isinstance(recurrence, dict)
    recurrence["commands"] = ["python recurrence_probe.py"]
    recurrence["predicates"] = [
        {"type": "command_exit_code", "command_index": 0, "equals": 0}
    ]

    with pytest.raises(ValueError, match="centralized_recurrence_commands_invalid"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )


def test_plan_owned_recurrence_remains_executable_and_bound() -> None:
    roles = _roles()
    roles["recurrence"] = {
        "description": "Run a verified problem-specific recurrence observation.",
        "commands": ["python recurrence_probe.py"],
        "command_bindings": [
            {
                "command_index": 0,
                "research_experiment_id": "experiment:recurrence",
            }
        ],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }

    parsed = parse_verification_contract_markdown(
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )
    )

    assert parsed is not None
    assert parsed["outcome_roles"]["recurrence"]["commands"] == [
        "python recurrence_probe.py"
    ]
    assert "verification_owner" not in parsed["outcome_roles"]["recurrence"]


def test_unlisted_live_runner_requires_research_experiment_binding() -> None:
    roles = _roles()
    live = roles["live"]
    assert isinstance(live, dict)
    live.pop("command_bindings")

    with pytest.raises(ValueError, match="command_bindings_invalid"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=roles,
        )


def test_v2_contract_hashes_server_bound_outcome_oracle() -> None:
    argv = ["python", ".usertest_research/repro.py"]
    argv_sha256 = hashlib.sha256(
        json.dumps(
            argv,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    positive_contract = {
        "schema_version": 1,
        "kind": "origin_evidence_semantic_contract",
        "research_experiment_id": "experiment:original",
        "mechanism_evidence_ids": ["mechanism_evidence:one"],
        "origin_evidence": {
            "atom_id": "atom:one",
            "atom_sha256": "d" * 64,
            "field_path": "$.expected_output",
            "value_sha256": "e" * 64,
        },
        "postconditions": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
    }
    positive_contract["positive_outcome_contract_id"] = (
        "positive_outcome_contract:"
        + hashlib.sha256(
            json.dumps(
                positive_contract,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    oracle = {
        "schema_version": 1,
        "case_id": "case:one",
        "repo_revision": "a" * 40,
        "research_experiment_id": "experiment:original",
        "scenario_kind": "original_replay",
        "origin_atom_ids": ["atom:one"],
        "mechanism_evidence_ids": ["mechanism_evidence:one"],
        "baseline": {
            "exit_code": 1,
            "observable_assertion": {
                "source": "stderr",
                "operator": "contains",
                "expected": "failure",
            },
            "stdout_sha256": "b" * 64,
            "stderr_sha256": "c" * 64,
        },
        "kind": "staged_replay",
        "proof_scope": "behavioral",
        "execution": {
            "argv": argv,
            "command_authorization": {
                "authorization_kind": "standard_test_or_research_harness",
                "executed_argv_sha256": argv_sha256,
                "shell": False,
                "workspace_confined": True,
            },
            "platform_requirement": "any",
            "shell": False,
        },
        "asset": None,
        "positive_outcome_contracts": [positive_contract],
    }
    canonical = json.dumps(
        oracle,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    oracle["outcome_oracle_id"] = "outcome_oracle:" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    roles = _roles()
    roles["original_scenario"] = {
        "description": "Replay the bound oracle.",
        "research_experiment_id": "experiment:original",
        "research_experiment_ids": ["experiment:original"],
        "selected_positive_outcome_contract_ids": [
            positive_contract["positive_outcome_contract_id"]
        ],
        "commands": [],
        "predicates": [
            {"type": "command_exit_code", "command_index": 0, "equals": 0}
        ],
        "oracle": oracle,
        "required_proof_scope": "behavioral",
    }

    markdown = render_verification_contract_markdown(
        ["python -m pytest tests/test_feature.py -q"],
        outcome_roles=roles,
    )
    parsed = parse_verification_contract_markdown(markdown)

    assert parsed is not None
    original = parsed["outcome_roles"]["original_scenario"]
    assert original["commands"] == []
    assert original["oracle"]["outcome_oracle_id"] == oracle["outcome_oracle_id"]
    assert original["required_proof_scope"] == "behavioral"
    forged = _roles()
    forged["original_scenario"] = {
        **roles["original_scenario"],
        "required_proof_scope": "configuration_state",
    }
    with pytest.raises(ValueError, match="oracle_scope_mismatch"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=forged,
        )
    unauthorized_oracle = json.loads(json.dumps(oracle))
    unauthorized_oracle.pop("outcome_oracle_id")
    unauthorized_oracle["execution"]["command_authorization"][
        "executed_argv_sha256"
    ] = "0" * 64
    unauthorized_oracle["outcome_oracle_id"] = "outcome_oracle:" + hashlib.sha256(
        json.dumps(
            unauthorized_oracle,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    unauthorized_roles = _roles()
    unauthorized_roles["original_scenario"] = {
        **roles["original_scenario"],
        "oracle": unauthorized_oracle,
    }
    with pytest.raises(ValueError, match="replay_oracle_invalid"):
        render_verification_contract_markdown(
            ["python -m pytest tests/test_feature.py -q"],
            outcome_roles=unauthorized_roles,
        )


def test_change_plan_round_trip_preserves_signed_multi_scenario_oracle() -> None:
    roles, expected_scenarios, expected_contracts = _multi_scenario_roles()

    change_plan_markdown = render_verification_contract_markdown(
        ["python -m pytest tests/test_feature.py -q"],
        outcome_roles=roles,
    )
    parsed_change_plan = parse_verification_contract_markdown(change_plan_markdown)

    assert parsed_change_plan is not None
    assert parsed_change_plan["schema_version"] == 2
    original = parsed_change_plan["outcome_roles"]["original_scenario"]
    assert original["research_experiment_ids"] == [
        "experiment:first",
        "experiment:second",
    ]
    assert original["selected_positive_outcome_contract_ids"] == [
        contract["positive_outcome_contract_id"] for contract in expected_contracts
    ]
    oracle = original["oracle"]
    assert oracle["kind"] == "multi_scenario"
    assert oracle["positive_outcome_contracts"] == expected_contracts
    assert oracle["scenarios"] == expected_scenarios
    assert [scenario["oracle"] for scenario in oracle["scenarios"]] == [
        scenario["oracle"] for scenario in expected_scenarios
    ]
    assert len(original["role_contract_sha256"]) == 64
    assert len(parsed_change_plan["contract_sha256"]) == 64

    tampered_change_plan = change_plan_markdown.replace(
        "Replay every retained original scenario.",
        "Replay only one retained original scenario.",
    )
    with pytest.raises(ValueError, match="outcome_role_contract_hash_mismatch"):
        parse_verification_contract_markdown(tampered_change_plan)
