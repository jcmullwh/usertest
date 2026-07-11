"""Authoritative readiness checks for stage-backed implementation tickets.

Queue stage names are routing metadata, not proof.  This module validates the complete
research -> option -> selection -> plan chain so every caller makes the same decision
about whether an implementation ticket is actually ready.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from backlog_core.stage_contracts import (
    assess_research_readiness,
    verified_deterministic_mechanism_closures,
    verified_hypothesis_falsification_attempts,
)

_SCOPE_LEVELS = frozenset({"single_path", "multiple_independent_paths", "shared_abstraction"})
_BREADTH_CONSUMER_KINDS = frozenset(
    {"production_entrypoint", "config_consumer", "live_failure_surface"}
)
_DISCOVERY_FIRST_RE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?"
    r"(?:locate|identify|determine|inspect|audit|review|investigate|find|explore|"
    r"discover|decide|choose|assess)\b",
    re.IGNORECASE,
)
_CLASS_SCOPE_RE = re.compile(
    r"\b(?:canonical|central(?:ize|ized|ization)|class[- ]level|shared(?:\s+internal)?"
    r"\s+(?:abstraction|contract|mechanism|source)|system[- ]wide|all\s+(?:callers|consumers))\b",
    re.IGNORECASE,
)
_RUNTIME_MARKERS = (
    "at runtime",
    "runtime failure",
    "live run",
    "run failure",
    "command failure",
    "shell unavailable",
    "shell probe",
    "docker daemon",
    "delivery failed",
    "cli execution",
)
_RUNTIME_BOUNDARY_RE = re.compile(
    r"\b(?:(?:live|deployed|production|remote|external)\s+"
    r"(?:integration|service|network)|service\s+(?:outage|unavailable)|"
    r"network\s+(?:failure|timeout|request))\b",
    re.IGNORECASE,
)
_EXTERNAL_PROVIDER_RUNTIME_RE = re.compile(
    r"(?=.*\b(?:claude|gemini|anthropic|openai)\b)"
    r"(?=.*\b(?:api|cli|provider|remote)\b)"
    r"(?=.*\b(?:failure|failures|failed|error|errors|message|response|stderr|"
    r"raw_events|agent_last_message)\b)",
    re.IGNORECASE,
)
_RUNTIME_ARTIFACT_KINDS = frozenset(
    {
        "agent_stderr",
        "command_failure",
        "runtime",
    }
)
_FALSIFICATION_EVIDENCE_EFFECTS = frozenset(
    {"supports_selection", "challenges_selection", "limits_scope"}
)
_MATERIAL_RISK_DISPOSITIONS = frozenset({"accepted", "mitigated", "blocks_selection"})
_OUTCOME_ROLES = frozenset({"original_scenario", "live", "mitigation_effect", "recurrence"})
_COMMAND_STREAMS = frozenset({"stdout", "stderr", "combined"})
_COMMAND_STREAM_OPERATORS = frozenset({"contains", "not_contains", "equals"})
_GENERIC_TEST_COMMAND_RE = re.compile(
    r"(?:^|[\s/\\])(?:pytest|unittest|ruff|mypy|pyright|eslint|jest|vitest|cargo\s+test|go\s+test)(?:\s|$)",
    re.IGNORECASE,
)
_PLAN_REVISION_FIELDS = (
    "change_plan_id",
    "case_id",
    "problem_id",
    "selected_option_id",
    "title",
    "problem",
    "user_impact",
    "proposed_fix",
    "repo_revision",
    "change_targets",
    "target_contract",
    "implementation_steps",
    "verification_steps",
    "verification_commands",
    "outcome_verification_roles",
    "before_after_reproduction",
    "compatibility_and_failure_modes",
    "causal_coverage",
    "scope_evidence",
    "requires_live_verification",
    "live_verification_rationale",
    "success_criteria",
    "rollback_notes",
    "suggested_owner",
    "related_change_plan_ids",
)


def _string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _string_list(value: Any, *, nonempty: bool = False) -> list[str] | None:
    if not isinstance(value, list):
        return None
    cleaned = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if len(cleaned) != len(value) or (nonempty and not cleaned):
        return None
    return cleaned


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _observable_assertion_predicate(assertion: Mapping[str, Any]) -> dict[str, Any] | None:
    source = _string(assertion.get("source"))
    operator = _string(assertion.get("operator"))
    expected = assertion.get("expected")
    if (
        source == "exit_code"
        and operator == "equals"
        and isinstance(expected, int)
        and not isinstance(expected, bool)
    ):
        return {"type": "command_exit_code", "command_index": 0, "equals": expected}
    if (
        source in _COMMAND_STREAMS
        and operator in _COMMAND_STREAM_OPERATORS
        and _string(expected) is not None
    ):
        return {
            "type": f"command_{source}_{operator}",
            "command_index": 0,
            "value": expected,
        }
    return None


def _artifact_expectation_predicate(
    expectation: Mapping[str, Any],
) -> dict[str, Any] | None:
    path = _string(expectation.get("path"))
    pointer = expectation.get("json_pointer")
    if (
        path is None
        or path.startswith(("/", "\\"))
        or ".." in path.replace("\\", "/").split("/")
        or not isinstance(pointer, str)
        or (pointer and not pointer.startswith("/"))
        or "equals" not in expectation
    ):
        return None
    return {
        "type": "artifact_json_value",
        "path": path,
        "json_pointer": pointer,
        "equals": expectation.get("equals"),
    }


def _research_dossier_members(
    research: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(research, Mapping):
        return []
    bundle = research.get("post_research_same_mechanism_bundle")
    if bundle is None:
        return [research]
    if not isinstance(bundle, Mapping):
        return []
    supplied_hash = _string(bundle.get("bundle_sha256"))
    projection = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    members = bundle.get("member_research_dossiers")
    if (
        supplied_hash != _canonical_sha256(projection)
        or not isinstance(members, list)
        or len(members) < 2
        or any(not isinstance(value, Mapping) for value in members)
    ):
        return []
    return [value for value in members if isinstance(value, Mapping)]


def verified_outcome_oracles(
    research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    """Return content-addressed runner oracles from a verified research receipt."""

    oracles: dict[str, Mapping[str, Any]] = {}
    for member in _research_dossier_members(research):
        verification = member.get("evidence_verification")
        if not isinstance(verification, Mapping) or verification.get("status") != "verified":
            continue
        raw = verification.get("outcome_oracles")
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, Mapping):
                continue
            oracle_id = _string(item.get("outcome_oracle_id"))
            projection = {key: value for key, value in item.items() if key != "outcome_oracle_id"}
            if oracle_id != f"outcome_oracle:{_canonical_sha256(projection)}":
                continue
            experiment_id = _string(item.get("research_experiment_id"))
            case_id = _string(item.get("case_id")) or _string(member.get("case_id"))
            if experiment_id is None:
                continue
            key = experiment_id
            if key in oracles:
                key = f"{case_id}::{experiment_id}"
            if key not in oracles:
                oracles[key] = item
    return oracles


def _verified_positive_outcome_contracts(
    oracle: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    raw = oracle.get("positive_outcome_contracts")
    verified: list[Mapping[str, Any]] = []
    for contract in raw if isinstance(raw, list) else []:
        if not isinstance(contract, Mapping):
            continue
        contract_id = _string(contract.get("positive_outcome_contract_id"))
        projection = {
            key: value for key, value in contract.items() if key != "positive_outcome_contract_id"
        }
        postconditions = contract.get("postconditions")
        if (
            contract_id != f"positive_outcome_contract:{_canonical_sha256(projection)}"
            or contract.get("kind")
            not in {
                "repository_test_assertion",
                "retained_research_harness_assertion",
                "origin_evidence_semantic_contract",
            }
            or not isinstance(postconditions, list)
            or not postconditions
            or any(not isinstance(item, Mapping) for item in postconditions)
        ):
            continue
        verified.append(contract)
    return verified


def _grounded_positive_predicates(
    oracle: Mapping[str, Any],
    *,
    selected_contract_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    predicates: list[dict[str, Any]] = []
    for contract in _verified_positive_outcome_contracts(oracle):
        contract_id = _string(contract.get("positive_outcome_contract_id"))
        if selected_contract_ids is not None and contract_id not in selected_contract_ids:
            continue
        for postcondition in contract.get("postconditions", []):
            if isinstance(postcondition, Mapping):
                predicate = dict(postcondition)
                if predicate not in predicates:
                    predicates.append(predicate)
    return predicates


def _research_positive_contract_index(
    research: Mapping[str, Any] | None,
) -> dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
    indexed: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for oracle in verified_outcome_oracles(research).values():
        for contract in _verified_positive_outcome_contracts(oracle):
            contract_id = _string(contract.get("positive_outcome_contract_id"))
            if contract_id is not None and contract_id not in indexed:
                indexed[contract_id] = (contract, oracle)
    return indexed


def _baseline_inverse_assertion(oracle: Mapping[str, Any]) -> dict[str, Any] | None:
    baseline = oracle.get("baseline")
    baseline = baseline if isinstance(baseline, Mapping) else {}
    assertion = baseline.get("observable_assertion")
    assertion = assertion if isinstance(assertion, Mapping) else {}
    source = _string(assertion.get("source"))
    operator = _string(assertion.get("operator"))
    expected = assertion.get("expected")
    if source not in _COMMAND_STREAMS or not isinstance(expected, str):
        return None
    inverse = "not_contains" if operator in {"contains", "equals"} else "contains"
    return {"source": source, "operator": inverse, "expected": expected}


def _runner_correct_value_assertions(
    research: Mapping[str, Any] | None,
    oracle: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Recover exact correct values already established by runner causal controls."""

    evidence = _verified_mechanism_evidence(research)
    controls = _verified_control_receipts(research)
    experiments = {
        str(item.get("experiment_id")): item
        for item in (research.get("experiments", []) if isinstance(research, Mapping) else [])
        if isinstance(item, Mapping) and _string(item.get("experiment_id")) is not None
    }
    assertions: list[dict[str, Any]] = []
    for evidence_id in _string_list(oracle.get("mechanism_evidence_ids")) or []:
        mechanism = evidence.get(evidence_id)
        control_id = (
            _string(mechanism.get("strong_pytest_control_id"))
            if isinstance(mechanism, Mapping)
            else None
        )
        control = controls.get(control_id or "")
        observable = control.get("observable_difference") if isinstance(control, Mapping) else None
        if (
            not isinstance(observable, Mapping)
            or observable.get("difference_kind") != "wrong_value_corrected"
        ):
            continue
        source = _string(observable.get("source"))
        expected_sha256 = _string(observable.get("control_expected_sha256"))
        control_experiment_id = _string(control.get("control_experiment_id"))
        experiment = experiments.get(control_experiment_id or "")
        declared = (
            experiment.get("observable_assertion") if isinstance(experiment, Mapping) else None
        )
        expected = declared.get("expected") if isinstance(declared, Mapping) else None
        if (
            source not in _COMMAND_STREAMS
            or expected_sha256 is None
            or not isinstance(declared, Mapping)
            or declared.get("source") != source
            or declared.get("operator") != "equals"
            or not isinstance(expected, str)
            or _canonical_sha256(expected) != expected_sha256
        ):
            continue
        assertion = {"source": source, "operator": "equals", "expected": expected}
        if assertion not in assertions:
            assertions.append(assertion)
    return assertions


def _selected_outcome_contract_ids(
    selection: Mapping[str, Any] | None,
    *,
    research: Mapping[str, Any],
) -> list[str]:
    positive_contracts = _research_positive_contract_index(research)
    if not positive_contracts:
        raise ValueError("research_positive_outcome_contract_missing")
    review = selection.get("falsification_review") if isinstance(selection, Mapping) else None
    selected = (
        _string_list(review.get("selected_positive_outcome_contract_ids"), nonempty=True)
        if isinstance(review, Mapping)
        else None
    )
    if selected is None and isinstance(review, Mapping):
        legacy = _string(review.get("selected_positive_outcome_contract_id"))
        selected = [legacy] if legacy is not None else None
    if selected is None and len(positive_contracts) == 1:
        selected = list(positive_contracts)
    if (
        selected is None
        or len(selected) != len(set(selected))
        or any(value not in positive_contracts for value in selected)
    ):
        raise ValueError("change_plan_selected_positive_outcome_contract_unbound")
    selected_oracles = [
        _string(positive_contracts[value][1].get("outcome_oracle_id")) for value in selected
    ]
    expected_oracles = {
        _string(oracle.get("outcome_oracle_id"))
        for _contract, oracle in positive_contracts.values()
    }
    if (
        None in selected_oracles
        or None in expected_oracles
        or len(selected_oracles) != len(set(selected_oracles))
        or set(selected_oracles) != expected_oracles
    ):
        raise ValueError("change_plan_selected_positive_outcome_oracle_coverage_mismatch")
    return selected


def _bind_single_outcome_scenario(
    oracle: Mapping[str, Any],
    *,
    selected_contract_id: str,
    after: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bound_after = dict(after) if isinstance(after, Mapping) else {}
    grounded_predicates = _grounded_positive_predicates(
        oracle,
        selected_contract_ids={selected_contract_id},
    )
    if not grounded_predicates:
        raise ValueError("research_selected_positive_outcome_contract_missing")
    predicates: list[dict[str, Any]] = []
    if oracle.get("kind") == "staged_replay":
        grounded_exits = {
            predicate.get("equals")
            for predicate in grounded_predicates
            if predicate.get("type") == "command_exit_code"
            and predicate.get("command_index") == 0
            and isinstance(predicate.get("equals"), int)
            and not isinstance(predicate.get("equals"), bool)
        }
        if len(grounded_exits) != 1:
            raise ValueError("research_positive_outcome_exit_contract_missing_or_conflicting")
        bound_after["expected_exit_code"] = next(iter(grounded_exits))
        assertions: list[dict[str, Any]] = []
        inverse = _baseline_inverse_assertion(oracle)
        if inverse is not None:
            assertions.append(inverse)
        for predicate in grounded_predicates:
            predicate_type = _string(predicate.get("type")) or ""
            if predicate_type == "command_exit_code":
                assertion = {
                    "source": "exit_code",
                    "operator": "equals",
                    "expected": predicate.get("equals"),
                }
            else:
                match = re.fullmatch(
                    r"command_(stdout|stderr|combined)_(contains|equals)",
                    predicate_type,
                )
                assertion = (
                    {
                        "source": match.group(1),
                        "operator": match.group(2),
                        "expected": predicate.get("value"),
                    }
                    if match is not None
                    else None
                )
            if isinstance(assertion, dict) and assertion not in assertions:
                assertions.append(assertion)
        bound_after["observable_assertions"] = assertions
        artifact_expectations = [
            {
                "path": predicate.get("path"),
                "json_pointer": predicate.get("json_pointer"),
                "equals": predicate.get("equals"),
            }
            for predicate in grounded_predicates
            if predicate.get("type") == "artifact_json_value"
        ]
        if artifact_expectations:
            bound_after["artifact_expectations"] = artifact_expectations
        else:
            bound_after.pop("artifact_expectations", None)
        predicates.extend(grounded_predicates)
        if inverse is not None:
            inverse_predicate = _observable_assertion_predicate(inverse)
            if inverse_predicate is not None and inverse_predicate not in predicates:
                predicates.append(inverse_predicate)
    elif oracle.get("kind") == "config_state":
        predicates.extend(
            predicate
            for predicate in grounded_predicates
            if predicate.get("type") == "oracle_state_equals"
        )
        bound_after["state_expectations"] = [
            {
                "target_id": predicate.get("target_id"),
                "exists": predicate.get("exists"),
                "equals": predicate.get("equals"),
            }
            for predicate in predicates
        ]
        bound_after.pop("observable_assertions", None)
        bound_after.pop("artifact_expectations", None)
    if not predicates:
        raise ValueError("change_plan_outcome_oracle_predicates_missing")
    return predicates, bound_after


def bind_plan_outcome_oracle(
    plan: Mapping[str, Any],
    *,
    research: Mapping[str, Any],
    selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind only stage-5-selected semantics to every retained original scenario."""

    bound = dict(plan)
    reproduction_raw = bound.get("before_after_reproduction")
    reproduction = dict(reproduction_raw) if isinstance(reproduction_raw, Mapping) else {}
    roles_raw = bound.get("outcome_verification_roles")
    roles = dict(roles_raw) if isinstance(roles_raw, Mapping) else {}
    original_raw = roles.get("original_scenario")
    original = dict(original_raw) if isinstance(original_raw, Mapping) else {}
    selected_ids = _selected_outcome_contract_ids(selection, research=research)
    positive_index = _research_positive_contract_index(research)
    scenarios: list[dict[str, Any]] = []
    for selected_id in selected_ids:
        _contract, oracle = positive_index[selected_id]
        predicates, scenario_after = _bind_single_outcome_scenario(
            oracle,
            selected_contract_id=selected_id,
            after=reproduction.get("after_change"),
        )
        scenario = {
            "positive_outcome_contract_id": selected_id,
            "oracle": dict(oracle),
            "predicates": predicates,
            "after_change": scenario_after,
        }
        scenario["scenario_id"] = "outcome_scenario:" + _canonical_sha256(scenario)
        scenarios.append(scenario)

    if len(scenarios) == 1:
        scenario = scenarios[0]
        oracle = scenario["oracle"]
        role_predicates = scenario["predicates"]
        reproduction["after_change"] = scenario["after_change"]
        bound_oracle = oracle
        experiment_ids = [_string(oracle.get("research_experiment_id"))]
    else:
        selected_contracts = [dict(positive_index[value][0]) for value in selected_ids]
        bound_oracle = {
            "schema_version": 1,
            "kind": "multi_scenario",
            "case_id": bound.get("case_id"),
            "repo_revision": research.get("repo_revision"),
            "proof_scope": "multi_scenario",
            "positive_outcome_contracts": selected_contracts,
            "scenarios": scenarios,
        }
        bound_oracle["outcome_oracle_id"] = "outcome_oracle:" + _canonical_sha256(bound_oracle)
        role_predicates = [
            {
                "type": "oracle_scenario_passed",
                "scenario_index": index,
                "scenario_id": scenario["scenario_id"],
            }
            for index, scenario in enumerate(scenarios)
        ]
        reproduction["after_change"] = {
            "scenario_expectations": [
                {
                    "scenario_id": scenario["scenario_id"],
                    "positive_outcome_contract_id": scenario["positive_outcome_contract_id"],
                    "after_change": scenario["after_change"],
                }
                for scenario in scenarios
            ]
        }
        experiment_ids = [
            _string(scenario["oracle"].get("research_experiment_id")) for scenario in scenarios
        ]
    if any(value is None for value in experiment_ids):
        raise ValueError("change_plan_outcome_oracle_experiment_identity_missing")
    original = {
        "description": _string(original.get("description"))
        or "Post-change replay of every retained runner-verified original scenario.",
        "research_experiment_id": experiment_ids[0],
        "research_experiment_ids": experiment_ids,
        "selected_positive_outcome_contract_ids": selected_ids,
        "commands": [],
        "predicates": role_predicates,
        "oracle": bound_oracle,
        "required_proof_scope": bound_oracle.get("proof_scope"),
    }
    roles["original_scenario"] = original
    bound["outcome_verification_roles"] = roles
    reproduction["research_experiment_id"] = experiment_ids[0]
    reproduction["research_experiment_ids"] = experiment_ids
    reproduction["outcome_oracle_id"] = bound_oracle.get("outcome_oracle_id")
    reproduction["outcome_oracle_ids"] = [
        scenario["oracle"].get("outcome_oracle_id") for scenario in scenarios
    ]
    reproduction["required_proof_scope"] = bound_oracle.get("proof_scope")
    bound["before_after_reproduction"] = reproduction
    return bound


def _oracle_inverts_baseline(
    baseline: Mapping[str, Any],
    oracle: Mapping[str, Any],
) -> bool:
    baseline_source = _string(baseline.get("source"))
    baseline_operator = _string(baseline.get("operator"))
    expected = baseline.get("expected")
    if baseline_source == "exit_code":
        return (
            baseline_operator == "equals"
            and isinstance(expected, int)
            and not isinstance(expected, bool)
            and expected != 0
            and oracle.get("source") == "exit_code"
            and oracle.get("operator") == "equals"
            and oracle.get("expected") == 0
        )
    if baseline_source not in _COMMAND_STREAMS or not isinstance(expected, str):
        return False
    inverse = "not_contains" if baseline_operator in {"contains", "equals"} else "contains"
    return (
        oracle.get("source") == baseline_source
        and oracle.get("operator") == inverse
        and oracle.get("expected") == expected
    )


def _positive_outcome_predicate(predicate: Mapping[str, Any]) -> bool:
    """Return whether a bound role predicate proves concrete successful behavior.

    Exit zero and removal of an error marker are necessary for many fixes, but either can
    be produced by swallowing the failing operation.  A resolved outcome therefore needs
    one positive stream value, artifact value, or runner-addressed state value as well.
    """

    predicate_type = _string(predicate.get("type"))
    if predicate_type in {
        f"command_{source}_{operator}"
        for source in _COMMAND_STREAMS
        for operator in ("contains", "equals")
    }:
        return _string(predicate.get("value")) is not None
    if predicate_type == "artifact_json_value":
        return (
            _string(predicate.get("path")) is not None
            and isinstance(predicate.get("json_pointer"), str)
            and "equals" in predicate
        )
    if predicate_type == "oracle_state_equals":
        return (
            _string(predicate.get("target_id")) is not None
            and isinstance(predicate.get("exists"), bool)
            and "equals" in predicate
        )
    return False


def _outcome_role_contract_errors(
    raw: Any,
    *,
    verification_commands: Sequence[str],
    reproduction: Mapping[str, Any] | None,
    requires_live: bool | None,
) -> list[str]:
    reasons: list[str] = []
    if not isinstance(raw, Mapping):
        return ["change_plan_outcome_roles_missing"]
    if set(raw) != _OUTCOME_ROLES:
        reasons.append("change_plan_outcome_role_fields_invalid")
    normalized_tests = {" ".join(command.split()) for command in verification_commands}
    normalized_roles: dict[str, Mapping[str, Any] | None] = {}
    for role in sorted(_OUTCOME_ROLES):
        value = raw.get(role)
        if value is None:
            normalized_roles[role] = None
            continue
        if not isinstance(value, Mapping):
            reasons.append(f"change_plan_outcome_role_invalid:{role}")
            normalized_roles[role] = None
            continue
        normalized_roles[role] = value
        if _string(value.get("description")) is None:
            reasons.append(f"change_plan_outcome_role_description_missing:{role}")
        oracle = value.get("oracle") if role == "original_scenario" else None
        oracle_kind = oracle.get("kind") if isinstance(oracle, Mapping) else None
        oracle_mode = oracle_kind in {"staged_replay", "config_state", "multi_scenario"}
        commands = _string_list(
            value.get("commands"), nonempty=role != "recurrence" and not oracle_mode
        )
        if commands is None:
            reasons.append(f"change_plan_outcome_role_commands_invalid:{role}")
            commands = []
        if oracle_mode and commands:
            reasons.append("change_plan_outcome_oracle_commands_forbidden")
        virtual_command_count = 1 if oracle_kind == "staged_replay" else len(commands)
        normalized_commands = {" ".join(command.split()) for command in commands}
        if role in {"live", "mitigation_effect", "recurrence"}:
            if normalized_commands.intersection(normalized_tests):
                reasons.append(f"change_plan_outcome_role_reuses_generic_verification:{role}")
            if any(_GENERIC_TEST_COMMAND_RE.search(command) for command in commands):
                reasons.append(f"change_plan_outcome_role_generic_test_forbidden:{role}")
        predicates = value.get("predicates")
        if not isinstance(predicates, list) or (not predicates and role != "recurrence"):
            reasons.append(f"change_plan_outcome_role_predicates_invalid:{role}")
            predicates = []
        exit_coverage: set[int] = set()
        for index, predicate in enumerate(predicates):
            if not isinstance(predicate, Mapping):
                reasons.append(f"change_plan_outcome_role_predicate_invalid:{role}:{index}")
                continue
            predicate_type = predicate.get("type")
            if predicate_type == "command_exit_code":
                command_index = predicate.get("command_index")
                equals = predicate.get("equals")
                if (
                    isinstance(command_index, bool)
                    or not isinstance(command_index, int)
                    or command_index < 0
                    or command_index >= virtual_command_count
                    or isinstance(equals, bool)
                    or not isinstance(equals, int)
                ):
                    reasons.append(
                        f"change_plan_outcome_role_exit_predicate_invalid:{role}:{index}"
                    )
                else:
                    exit_coverage.add(command_index)
            elif predicate_type in {
                f"command_{source}_{operator}"
                for source in _COMMAND_STREAMS
                for operator in _COMMAND_STREAM_OPERATORS
            }:
                command_index = predicate.get("command_index")
                if (
                    isinstance(command_index, bool)
                    or not isinstance(command_index, int)
                    or command_index < 0
                    or command_index >= virtual_command_count
                    or _string(predicate.get("value")) is None
                ):
                    reasons.append(
                        f"change_plan_outcome_role_stream_predicate_invalid:{role}:{index}"
                    )
            elif predicate_type == "artifact_json_value":
                path = _string(predicate.get("path"))
                pointer = predicate.get("json_pointer")
                if (
                    path is None
                    or path.startswith(("/", "\\"))
                    or ".." in path.replace("\\", "/").split("/")
                    or not isinstance(pointer, str)
                    or (pointer and not pointer.startswith("/"))
                    or "equals" not in predicate
                ):
                    reasons.append(
                        f"change_plan_outcome_role_artifact_predicate_invalid:{role}:{index}"
                    )
            elif predicate_type == "oracle_state_equals":
                target_id = _string(predicate.get("target_id"))
                if (
                    oracle_kind != "config_state"
                    or target_id is None
                    or not isinstance(predicate.get("exists"), bool)
                    or "equals" not in predicate
                ):
                    reasons.append(
                        f"change_plan_outcome_role_state_predicate_invalid:{role}:{index}"
                    )
            elif predicate_type == "oracle_scenario_passed":
                scenario_index = predicate.get("scenario_index")
                scenarios = (
                    oracle.get("scenarios")
                    if isinstance(oracle, Mapping) and isinstance(oracle.get("scenarios"), list)
                    else []
                )
                if (
                    oracle_kind != "multi_scenario"
                    or isinstance(scenario_index, bool)
                    or not isinstance(scenario_index, int)
                    or scenario_index < 0
                    or scenario_index >= len(scenarios)
                    or not isinstance(scenarios[scenario_index], Mapping)
                    or _string(predicate.get("scenario_id"))
                    != _string(scenarios[scenario_index].get("scenario_id"))
                ):
                    reasons.append(
                        f"change_plan_outcome_role_scenario_predicate_invalid:{role}:{index}"
                    )
            else:
                reasons.append(f"change_plan_outcome_role_predicate_type_invalid:{role}:{index}")
        expected_exit_coverage = (
            {0} if oracle_kind == "staged_replay" else set(range(len(commands)))
        )
        if exit_coverage != expected_exit_coverage:
            reasons.append(f"change_plan_outcome_role_exit_coverage_invalid:{role}")
        if oracle_mode:
            oracle_id = _string(oracle.get("outcome_oracle_id"))
            projection = {key: item for key, item in oracle.items() if key != "outcome_oracle_id"}
            if oracle_id != f"outcome_oracle:{_canonical_sha256(projection)}":
                reasons.append("change_plan_outcome_oracle_hash_invalid")
            if value.get("required_proof_scope") != oracle.get("proof_scope"):
                reasons.append("change_plan_outcome_oracle_scope_mismatch")
            if oracle_kind == "staged_replay" and oracle.get("proof_scope") != "behavioral":
                reasons.append("change_plan_outcome_replay_scope_invalid")
            if oracle_kind == "config_state" and oracle.get("proof_scope") != "configuration_state":
                reasons.append("change_plan_outcome_config_scope_invalid")
            if oracle_kind == "multi_scenario" and oracle.get("proof_scope") != "multi_scenario":
                reasons.append("change_plan_outcome_multi_scenario_scope_invalid")

    original = normalized_roles.get("original_scenario")
    if original is None:
        reasons.append("change_plan_outcome_original_role_required")
    elif reproduction is not None:
        after = reproduction.get("after_change")
        after = after if isinstance(after, Mapping) else {}
        original_oracle = original.get("oracle")
        oracle_kind = original_oracle.get("kind") if isinstance(original_oracle, Mapping) else None
        role_commands = _string_list(original.get("commands"), nonempty=oracle_kind is None) or []
        after_command = _string(after.get("command"))
        if oracle_kind is None:
            if len(role_commands) != 1 or (
                after_command is not None
                and " ".join(role_commands[0].split()) != " ".join(after_command.split())
            ):
                reasons.append("change_plan_outcome_original_command_mismatch")
        elif role_commands:
            reasons.append("change_plan_outcome_original_oracle_commands_present")
        if _string(original.get("research_experiment_id")) != _string(
            reproduction.get("research_experiment_id")
        ):
            reasons.append("change_plan_outcome_original_experiment_mismatch")
        if isinstance(original_oracle, Mapping):
            if _string(original_oracle.get("outcome_oracle_id")) != _string(
                reproduction.get("outcome_oracle_id")
            ):
                reasons.append("change_plan_outcome_original_oracle_mismatch")
            if original.get("required_proof_scope") != reproduction.get("required_proof_scope"):
                reasons.append("change_plan_outcome_original_scope_mismatch")
        expected_exit = after.get("expected_exit_code")
        original_predicates = original.get("predicates")
        original_exit_matches = any(
            isinstance(predicate, Mapping)
            and predicate.get("type") == "command_exit_code"
            and predicate.get("command_index") == 0
            and predicate.get("equals") == expected_exit
            for predicate in (original_predicates if isinstance(original_predicates, list) else [])
        )
        if oracle_kind not in {"config_state", "multi_scenario"} and not original_exit_matches:
            reasons.append("change_plan_outcome_original_predicate_mismatch")
        after_assertions_raw = after.get("observable_assertions")
        after_assertions = after_assertions_raw if isinstance(after_assertions_raw, list) else []
        expected_predicates = [
            predicate
            for assertion in after_assertions
            if isinstance(assertion, Mapping)
            for predicate in [_observable_assertion_predicate(assertion)]
            if predicate is not None
        ]
        artifact_expectations = after.get("artifact_expectations")
        expected_predicates.extend(
            predicate
            for expectation in (
                artifact_expectations if isinstance(artifact_expectations, list) else []
            )
            if isinstance(expectation, Mapping)
            for predicate in [_artifact_expectation_predicate(expectation)]
            if predicate is not None
        )
        original_predicate_list = (
            original_predicates if isinstance(original_predicates, list) else []
        )
        if any(predicate not in original_predicate_list for predicate in expected_predicates):
            reasons.append("change_plan_outcome_original_observable_oracle_mismatch")
        if oracle_kind == "config_state":
            targets = {
                str(target.get("target_id")): target
                for target in original_oracle.get("state_targets", [])
                if isinstance(target, Mapping) and _string(target.get("target_id")) is not None
            }
            state_predicates = [
                predicate
                for predicate in original_predicate_list
                if isinstance(predicate, Mapping) and predicate.get("type") == "oracle_state_equals"
            ]
            if not state_predicates or {
                str(predicate.get("target_id")) for predicate in state_predicates
            } != set(targets):
                reasons.append("change_plan_outcome_config_target_coverage_invalid")
            for predicate in state_predicates:
                target = targets.get(str(predicate.get("target_id")))
                if target is None or (
                    predicate.get("exists") is True
                    and predicate.get("equals") == target.get("baseline_value")
                ):
                    reasons.append("change_plan_outcome_config_state_not_changed")
        if oracle_kind == "multi_scenario":
            scenarios = original_oracle.get("scenarios")
            scenario_expectations = after.get("scenario_expectations")
            if (
                not isinstance(scenarios, list)
                or len(scenarios) < 2
                or not isinstance(scenario_expectations, list)
                or len(scenario_expectations) != len(scenarios)
                or [
                    _string(value.get("scenario_id"))
                    for value in scenario_expectations
                    if isinstance(value, Mapping)
                ]
                != [
                    _string(value.get("scenario_id"))
                    for value in scenarios
                    if isinstance(value, Mapping)
                ]
            ):
                reasons.append("change_plan_outcome_multi_scenario_coverage_invalid")
    if normalized_roles.get("recurrence") is None:
        reasons.append("change_plan_outcome_recurrence_role_required")
    if requires_live is True and normalized_roles.get("live") is None:
        reasons.append("change_plan_outcome_live_role_required")
    if requires_live is False and normalized_roles.get("live") is not None:
        reasons.append("change_plan_outcome_live_role_unjustified")
    expected_outcome_state = (
        reproduction.get("expected_outcome_state") if isinstance(reproduction, Mapping) else None
    )
    if expected_outcome_state == "resolved":
        original_predicates = (
            original.get("predicates")
            if isinstance(original, Mapping) and isinstance(original.get("predicates"), list)
            else []
        )
        original_oracle = original.get("oracle") if isinstance(original, Mapping) else None
        grounded_predicates = (
            _grounded_positive_predicates(original_oracle)
            if isinstance(original_oracle, Mapping)
            else []
        )
        assertion_exit_is_semantic = any(
            contract.get("kind")
            in {
                "repository_test_assertion",
                "retained_research_harness_assertion",
            }
            for contract in (
                _verified_positive_outcome_contracts(original_oracle)
                if isinstance(original_oracle, Mapping)
                else []
            )
        )
        multi_scenario_positive = bool(
            isinstance(original_oracle, Mapping)
            and original_oracle.get("kind") == "multi_scenario"
            and isinstance(original_oracle.get("scenarios"), list)
            and original_oracle.get("scenarios")
            and all(
                isinstance(scenario, Mapping)
                and any(
                    isinstance(predicate, Mapping) and _positive_outcome_predicate(predicate)
                    for predicate in (
                        scenario.get("predicates")
                        if isinstance(scenario.get("predicates"), list)
                        else []
                    )
                )
                for scenario in original_oracle.get("scenarios", [])
            )
        )
        if not multi_scenario_positive and not any(
            isinstance(predicate, Mapping)
            and dict(predicate) in grounded_predicates
            and (
                _positive_outcome_predicate(predicate)
                or (
                    assertion_exit_is_semantic
                    and predicate.get("type") == "command_exit_code"
                    and predicate.get("command_index") == 0
                    and predicate.get("equals") == 0
                )
            )
            for predicate in original_predicates
        ):
            reasons.append("change_plan_positive_outcome_contract_missing_research_required")
    if expected_outcome_state == "mitigated" and normalized_roles.get("mitigation_effect") is None:
        reasons.append("change_plan_outcome_mitigation_role_required")
    return reasons


def _content_address_matches(
    receipt: Mapping[str, Any],
    *,
    id_field: str,
    prefix: str,
) -> bool:
    receipt_id = _string(receipt.get(id_field))
    projection = {key: value for key, value in receipt.items() if key != id_field}
    return receipt_id == f"{prefix}:{_canonical_sha256(projection)}"


def _verified_control_receipts(
    research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    verified: dict[str, Mapping[str, Any]] = {}
    controls = [
        control
        for member in _research_dossier_members(research)
        for verification in [member.get("evidence_verification")]
        if isinstance(verification, Mapping) and verification.get("status") == "verified"
        for control in (
            verification.get("control_verifications")
            if isinstance(verification.get("control_verifications"), list)
            else []
        )
    ]
    for control in controls:
        if (
            not isinstance(control, Mapping)
            or control.get("verification_method") != "pytest_ast_controlled_difference_v2"
            or control.get("adversarial_effect") != "limits_scope"
            or not isinstance(control.get("controlled_input_difference"), Mapping)
            or not isinstance(control.get("observable_difference"), Mapping)
            or not _content_address_matches(
                control,
                id_field="control_verification_id",
                prefix="control_verification",
            )
        ):
            continue
        control_id = _string(control.get("control_verification_id"))
        if control_id is not None and control_id not in verified:
            verified[control_id] = control
    return verified


def _verified_mechanism_evidence(
    research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    """Return content-addressed runner evidence across all supported proof modes."""

    verified: dict[str, Mapping[str, Any]] = {}
    allowed_types = {
        "exception_trace",
        "observed_output",
        "controlled_scenario",
        "temporary_harness",
        "static_trace",
        "live_runtime",
    }
    raw = [
        evidence
        for member in _research_dossier_members(research)
        for verification in [member.get("evidence_verification")]
        if isinstance(verification, Mapping) and verification.get("status") == "verified"
        for evidence in (
            verification.get("mechanism_evidence")
            if isinstance(verification.get("mechanism_evidence"), list)
            else []
        )
    ]
    for evidence in raw:
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("evidence_type") not in allowed_types
            or not _content_address_matches(
                evidence,
                id_field="mechanism_evidence_id",
                prefix="mechanism_evidence",
            )
        ):
            continue
        evidence_id = _string(evidence.get("mechanism_evidence_id"))
        if evidence_id is not None and evidence_id not in verified:
            verified[evidence_id] = evidence
    return verified


def verified_mechanism_evidence(
    research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    """Public read-only projection of runner-verified typed mechanism evidence."""

    return dict(_verified_mechanism_evidence(research))


def _verified_selected_falsification_attempts(
    research: Mapping[str, Any] | None,
    *,
    hypothesis_id: str,
    attempt_refs: Sequence[str],
) -> list[dict[str, Any]]:
    """Project exact causal challenges bound by the selected option."""

    if not isinstance(research, dict):
        return []
    hypothesis = _research_hypotheses(research).get(hypothesis_id)
    attempts_raw = (
        hypothesis.get("falsification_attempts") if isinstance(hypothesis, Mapping) else None
    )
    declared_refs = [
        attempt_id
        for attempt in (attempts_raw if isinstance(attempts_raw, list) else [])
        if isinstance(attempt, Mapping)
        for attempt_id in [_string(attempt.get("attempt_id"))]
        if attempt_id is not None
    ]
    if list(attempt_refs) != declared_refs:
        return []
    verified = verified_hypothesis_falsification_attempts(
        research,
        hypothesis_id=hypothesis_id,
    )
    verified_by_id = {
        str(attempt.get("attempt_id")): attempt
        for attempt in verified
        if isinstance(attempt, Mapping)
    }
    return [
        dict(verified_by_id[attempt_id])
        for attempt_id in attempt_refs
        if attempt_id in verified_by_id
    ]


def _verified_selected_deterministic_closures(
    research: Mapping[str, Any] | None,
    *,
    hypothesis_id: str,
    closure_refs: Sequence[str],
) -> list[dict[str, Any]]:
    """Project exact runner-minted deterministic closures bound by an option."""

    verified = verified_deterministic_mechanism_closures(
        dict(research) if isinstance(research, Mapping) else None,
        hypothesis_id=hypothesis_id,
    )
    verified_by_id = {
        str(closure.get("closure_receipt_id")): closure
        for closure in verified
        if isinstance(closure, Mapping) and _string(closure.get("closure_receipt_id")) is not None
    }
    expected_refs = sorted(verified_by_id)
    if list(closure_refs) != expected_refs:
        return []
    return [dict(verified_by_id[closure_id]) for closure_id in closure_refs]


def falsification_acceptance_has_adversarial_basis(
    review: Mapping[str, Any],
) -> bool:
    """Return whether runner proof supports a challenge or deterministic closure."""

    receipt = review.get("adversarial_evidence_receipt")
    attempts = receipt.get("falsification_attempts") if isinstance(receipt, Mapping) else None
    if any(
        isinstance(attempt, Mapping) and attempt.get("outcome") == "survived"
        for attempt in (attempts if isinstance(attempts, list) else [])
    ):
        return True
    closures = (
        receipt.get("deterministic_mechanism_closures") if isinstance(receipt, Mapping) else None
    )
    return bool(closures) and all(
        isinstance(closure, Mapping)
        and closure.get("verification_method") == "runner_deterministic_mechanism_closure_v1"
        for closure in (closures if isinstance(closures, list) else [])
    )


def _verified_failure_paths(
    research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    controls = _verified_control_receipts(research)
    paths = [
        path
        for member in _research_dossier_members(research)
        for verification in [member.get("evidence_verification")]
        if isinstance(verification, Mapping) and verification.get("status") == "verified"
        for path in (
            verification.get("failure_paths")
            if isinstance(verification.get("failure_paths"), list)
            else []
        )
    ]
    verified: dict[str, Mapping[str, Any]] = {}
    for path in paths:
        consumer_identity = path.get("consumer_identity") if isinstance(path, Mapping) else None
        origin_atom_ids = _string_list(
            path.get("origin_atom_ids") if isinstance(path, Mapping) else None,
            nonempty=True,
        )
        if (
            not isinstance(path, Mapping)
            or path.get("verification_method") != "runner_controlled_failure_path_v1"
            or _string(path.get("path_name")) is None
            or not isinstance(consumer_identity, Mapping)
            or _string(consumer_identity.get("entrypoint")) != _string(path.get("path_name"))
            or _string(path.get("independence_key")) != _canonical_sha256(consumer_identity)
            or _string(path.get("control_verification_id")) not in controls
            or _string(path.get("hypothesis_id")) is None
            or _string_list(path.get("mechanism_symbols"), nonempty=True) is None
            or origin_atom_ids is None
            or not isinstance(path.get("observed_failure"), Mapping)
            or not _content_address_matches(
                path,
                id_field="failure_path_id",
                prefix="failure_path",
            )
        ):
            continue
        path_id = _string(path.get("failure_path_id"))
        if path_id is not None and path_id not in verified:
            verified[path_id] = path
    # Typed mechanism evidence also represents an independently observed path.
    # This lets breadth claims cite a real runtime/config/harness/static path
    # instead of requiring every consumer to fit the pytest-AST control shape.
    for evidence_id, evidence in _verified_mechanism_evidence(research).items():
        consumer_identity = evidence.get("consumer_identity")
        if (
            _string(evidence.get("path_name")) is not None
            and isinstance(consumer_identity, Mapping)
            and _string(consumer_identity.get("kind")) is not None
            and _string(consumer_identity.get("entrypoint")) == _string(evidence.get("path_name"))
            and _string(evidence.get("independence_key")) == _canonical_sha256(consumer_identity)
            and _string(evidence.get("hypothesis_id")) is not None
            and _string_list(evidence.get("mechanism_symbols"), nonempty=True) is not None
            and _string_list(evidence.get("origin_atom_ids"), nonempty=True) is not None
        ):
            verified.setdefault(evidence_id, evidence)
    return verified


def _mechanism_link_symbols(link: Any) -> set[str]:
    """Project only symbols tied to an observable by a runner-derived link."""

    if not isinstance(link, Mapping):
        return set()
    method = _string(link.get("verification_method"))
    if method in {
        "runner_python_call_chain_v1",
        "runner_exception_symbol_trace_v1",
        "runner_deterministic_static_trace_v1",
    }:
        code_path = link.get("code_path")
        return {
            symbol
            for step in (code_path if isinstance(code_path, list) else [])
            if isinstance(step, Mapping)
            for symbol in [_string(step.get("symbol"))]
            if symbol is not None
        }
    if method == "runner_harness_observable_dataflow_v1":
        sinks = link.get("symbol_sinks")
        return {
            symbol
            for sink in (sinks if isinstance(sinks, list) else [])
            if isinstance(sink, Mapping) and _string(sink.get("sink")) is not None
            for symbol in [_string(sink.get("symbol"))]
            if symbol is not None
        }
    return set()


def _verified_intervention_path_keys(
    *,
    research: Mapping[str, Any] | None,
    hypothesis_id: str,
    mechanism_symbols: Sequence[str],
    target_path: str,
    target_symbol: str,
    controlled_symbols: Sequence[str],
) -> set[str]:
    """Return observed path keys on which the selected intervention is grounded.

    This deliberately proves a causal change boundary, not a PR file allowlist.  The
    selected target may be the only production edit even when the verified mechanism
    traverses several symbols.
    """

    expected_symbols = list(mechanism_symbols)
    controlled = set(controlled_symbols)
    controls = _verified_control_receipts(research)
    keys: set[str] = set()
    for evidence in _verified_mechanism_evidence(research).values():
        if (
            _string(evidence.get("hypothesis_id")) != hypothesis_id
            or _string_list(evidence.get("mechanism_symbols"), nonempty=True) != expected_symbols
        ):
            continue
        code_paths = evidence.get("code_paths")
        if not any(
            isinstance(point, Mapping)
            and _string(point.get("path")) == target_path
            and _string(point.get("symbol")) == target_symbol
            for point in (code_paths if isinstance(code_paths, list) else [])
        ):
            continue
        linked_symbols = _mechanism_link_symbols(evidence.get("mechanism_link"))
        link_covers = controlled.issubset(linked_symbols)
        strong_control_id = _string(evidence.get("strong_pytest_control_id"))
        strong_control = controls.get(strong_control_id or "")
        strong_symbols = (
            _string_list(
                strong_control.get("shared_verified_mechanism_symbols"),
                nonempty=True,
            )
            if isinstance(strong_control, Mapping)
            else None
        )
        strong_control_covers = (
            isinstance(strong_control, Mapping)
            and _string(strong_control.get("hypothesis_id")) == hypothesis_id
            and _string_list(strong_control.get("mechanism_symbols"), nonempty=True)
            == expected_symbols
            and strong_symbols == expected_symbols
            and controlled.issubset(set(strong_symbols or []))
        )
        independence_key = _string(evidence.get("independence_key"))
        if independence_key is not None and (link_covers or strong_control_covers):
            keys.add(independence_key)
    return keys


def _intervention_sufficiency_reasons(
    coverage: Mapping[str, Any],
    *,
    research: Mapping[str, Any] | None,
    bound_scope_paths: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Require multi-symbol sufficiency to cover every selected observed path."""

    binding = coverage.get("research_binding")
    if not isinstance(binding, Mapping):
        return []
    hypothesis_id = _string(binding.get("hypothesis_id"))
    mechanism_symbols = _string_list(binding.get("mechanism_symbols"), nonempty=True)
    if hypothesis_id is None or mechanism_symbols is None or len(mechanism_symbols) <= 1:
        return []
    required_path_keys = {
        key
        for path in bound_scope_paths
        for key in [_string(path.get("independence_key"))]
        if key is not None
    }
    if not required_path_keys:
        return []
    points_raw = binding.get("intervention_points")
    points = points_raw if isinstance(points_raw, list) else []
    reasons: list[str] = []
    for index, point in enumerate(points):
        if not isinstance(point, Mapping) or _string(point.get("causal_role")) != (
            "sufficient_control_point"
        ):
            continue
        controlled_symbols = _string_list(point.get("controls_mechanism_symbols"), nonempty=True)
        target_path = _string(point.get("target_path"))
        target_symbol = _string(point.get("target_symbol"))
        if (
            controlled_symbols is None
            or set(controlled_symbols) != set(mechanism_symbols)
            or target_path is None
            or target_symbol is None
        ):
            continue
        verified_path_keys = _verified_intervention_path_keys(
            research=research,
            hypothesis_id=hypothesis_id,
            mechanism_symbols=mechanism_symbols,
            target_path=target_path,
            target_symbol=target_symbol,
            controlled_symbols=controlled_symbols,
        )
        if not required_path_keys.issubset(verified_path_keys):
            reasons.append(f"solution_option_intervention_sufficiency_unverified:{index}")
    return reasons


def _broad_scope_outcome_coverage_reasons(
    plan: Mapping[str, Any],
    *,
    selected_option: Mapping[str, Any] | None,
    research: Mapping[str, Any] | None,
) -> list[str]:
    """Require one post-change proof path for every evidenced class-level path.

    A shared-abstraction plan may legitimately use one replay when that replay's
    runner-bound mechanism evidence spans every selected independence key.  It may
    also need multiple research oracles, but until those are bound it must remain
    non-ready.  Single-path plans deliberately bypass this class-level gate.
    """

    if not isinstance(selected_option, Mapping):
        return []
    scope = selected_option.get("scope_evidence")
    if not isinstance(scope, Mapping) or scope.get("scope_level") not in {
        "multiple_independent_paths",
        "shared_abstraction",
    }:
        return []
    verified_paths = _verified_failure_paths(research)
    required_keys: set[str] = set()
    paths_raw = scope.get("independent_consumers_or_failure_paths")
    for path in paths_raw if isinstance(paths_raw, list) else []:
        refs = _string_list(
            path.get("evidence_refs") if isinstance(path, Mapping) else None,
            nonempty=True,
        )
        if refs is None or len(refs) != 1:
            continue
        receipt = verified_paths.get(refs[0])
        independence_key = (
            _string(receipt.get("independence_key")) if isinstance(receipt, Mapping) else None
        )
        if independence_key is not None:
            required_keys.add(independence_key)
    if len(required_keys) < 2:
        return []

    roles = plan.get("outcome_verification_roles")
    original = roles.get("original_scenario") if isinstance(roles, Mapping) else None
    oracle = original.get("oracle") if isinstance(original, Mapping) else None
    oracle_evidence_refs = _string_list(
        oracle.get("mechanism_evidence_ids") if isinstance(oracle, Mapping) else None,
        nonempty=True,
    )
    evidence = _verified_mechanism_evidence(research)
    covered_keys = {
        independence_key
        for evidence_id in (oracle_evidence_refs or [])
        for receipt in [evidence.get(evidence_id)]
        if isinstance(receipt, Mapping)
        for independence_key in [_string(receipt.get("independence_key"))]
        if independence_key is not None
    }
    if not required_keys.issubset(covered_keys):
        return ["change_plan_broad_scope_outcome_path_coverage_missing"]
    return []


def bind_falsification_review(
    review: Mapping[str, Any],
    *,
    problem_id: str,
    selected_option: Mapping[str, Any],
    research: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind a model review to runner-proven controls and derive adversarial effects.

    The model may explain what a control means, but it cannot label arbitrary evidence
    as challenging.  Only content-addressed causal controls for the selected mechanism
    are accepted, and their effect is overwritten from the runner receipt.
    """

    selected_option_id = _string(selected_option.get("option_id"))
    if selected_option_id is None:
        raise ValueError("falsification_selected_option_identity_missing")
    if _string(review.get("problem_id")) != problem_id:
        raise ValueError("falsification_problem_id_mismatch")
    if _string(review.get("selected_option_id")) != selected_option_id:
        raise ValueError("falsification_option_id_mismatch")
    coverage = selected_option.get("causal_coverage")
    binding = coverage.get("research_binding") if isinstance(coverage, Mapping) else None
    hypothesis_id = _string(binding.get("hypothesis_id")) if isinstance(binding, Mapping) else None
    mechanism_symbols = (
        _string_list(binding.get("mechanism_symbols"), nonempty=True)
        if isinstance(binding, Mapping)
        else None
    )
    if hypothesis_id is None or mechanism_symbols is None:
        raise ValueError("falsification_selected_option_research_binding_missing")
    evidence_pool = {
        evidence_id: evidence
        for evidence_id, evidence in _verified_mechanism_evidence(research).items()
        if _string(evidence.get("hypothesis_id")) == hypothesis_id
        and _string_list(evidence.get("mechanism_symbols"), nonempty=True) == mechanism_symbols
    }
    if not evidence_pool:
        raise ValueError("falsification_verified_mechanism_evidence_missing")
    attempt_refs = (
        _string_list(binding.get("falsification_attempt_refs"))
        if isinstance(binding, Mapping)
        else None
    )
    closure_refs = (
        _string_list(binding.get("deterministic_closure_refs"))
        if isinstance(binding, Mapping)
        else None
    )
    verified_attempts = _verified_selected_falsification_attempts(
        research,
        hypothesis_id=hypothesis_id,
        attempt_refs=attempt_refs or [],
    )
    verified_closures = _verified_selected_deterministic_closures(
        research,
        hypothesis_id=hypothesis_id,
        closure_refs=closure_refs or [],
    )
    if verified_attempts and verified_closures:
        raise ValueError("falsification_research_proof_route_invalid")
    verification = research.get("evidence_verification") if isinstance(research, Mapping) else None
    research_receipt_sha256 = (
        _string(verification.get("receipt_sha256")) if isinstance(verification, Mapping) else None
    )
    if research_receipt_sha256 is None:
        raise ValueError("falsification_research_receipt_hash_missing")

    overall_verdict = _string(review.get("verdict"))
    positive_contracts = _research_positive_contract_index(research)
    reviews_raw = review.get("outcome_contract_reviews")
    selected_contract_id = _string(review.get("selected_positive_outcome_contract_id"))
    selected_contract_ids_raw = review.get("selected_positive_outcome_contract_ids")
    selected_contract_ids = (
        _string_list(selected_contract_ids_raw, nonempty=True)
        if selected_contract_ids_raw is not None
        else ([selected_contract_id] if selected_contract_id is not None else None)
    )
    contract_reviews: list[dict[str, Any]] = []
    reviewed_ids: set[str] = set()
    if not positive_contracts:
        if overall_verdict == "accept":
            raise ValueError("falsification_positive_outcome_contract_missing")
        if selected_contract_ids is not None or reviews_raw not in (None, []):
            raise ValueError("falsification_outcome_contract_review_without_contract")
        selected_contract_id = None
        selected_contract_ids = []
    else:
        if not isinstance(reviews_raw, list) or not reviews_raw:
            raise ValueError("falsification_outcome_contract_reviews_missing")
        if (
            selected_contract_ids is None
            or len(selected_contract_ids) != len(set(selected_contract_ids))
            or any(value not in positive_contracts for value in selected_contract_ids)
        ):
            raise ValueError("falsification_selected_outcome_contract_unbound")
        for index, contract_review in enumerate(reviews_raw):
            if not isinstance(contract_review, Mapping):
                raise ValueError(f"falsification_outcome_contract_review_invalid:{index}")
            contract_id = _string(contract_review.get("positive_outcome_contract_id"))
            verdict = _string(contract_review.get("verdict"))
            relation = _string(contract_review.get("semantic_relation_assessment"))
            coverage = _string(contract_review.get("problem_coverage"))
            intended = contract_review.get("proves_intended_operation")
            residual = _string_list(contract_review.get("residual_untested_paths"))
            refs = _string_list(contract_review.get("evidence_refs"), nonempty=True)
            if contract_id not in positive_contracts or contract_id in reviewed_ids:
                raise ValueError(f"falsification_outcome_contract_review_unbound:{index}")
            if verdict not in {
                "sufficient",
                "surface_only",
                "insufficient_evidence",
                "contradicted",
            }:
                raise ValueError(f"falsification_outcome_contract_verdict_invalid:{index}")
            if relation is None:
                raise ValueError(f"falsification_outcome_contract_relation_missing:{index}")
            if coverage not in {"full", "partial", "unknown"}:
                raise ValueError(f"falsification_outcome_contract_coverage_invalid:{index}")
            if not isinstance(intended, bool):
                raise ValueError(f"falsification_outcome_contract_operation_invalid:{index}")
            if residual is None or refs is None:
                raise ValueError(f"falsification_outcome_contract_evidence_invalid:{index}")
            if any(ref not in evidence_pool for ref in refs):
                raise ValueError(f"falsification_outcome_contract_evidence_unbound:{index}")
            contract, oracle = positive_contracts[contract_id]
            reviewed_ids.add(contract_id)
            contract_reviews.append(
                {
                    **dict(contract_review),
                    "positive_outcome_contract_id": contract_id,
                    "verdict": verdict,
                    "semantic_relation_assessment": relation,
                    "problem_coverage": coverage,
                    "proves_intended_operation": intended,
                    "residual_untested_paths": residual,
                    "evidence_refs": refs,
                    "positive_outcome_contract_sha256": _canonical_sha256(contract),
                    "outcome_oracle_id": oracle.get("outcome_oracle_id"),
                }
            )
        if reviewed_ids != set(positive_contracts):
            raise ValueError("falsification_outcome_contract_review_coverage_mismatch")
        oracle_ids = {
            _string(oracle.get("outcome_oracle_id"))
            for _contract, oracle in positive_contracts.values()
        }
        selected_oracle_ids = [
            _string(positive_contracts[value][1].get("outcome_oracle_id"))
            for value in selected_contract_ids
        ]
        if (
            None in oracle_ids
            or None in selected_oracle_ids
            or set(selected_oracle_ids) != oracle_ids
            or len(selected_oracle_ids) != len(oracle_ids)
        ):
            raise ValueError("falsification_selected_outcome_contract_oracle_coverage_mismatch")
        selected_reviews = [
            value
            for value in contract_reviews
            if value["positive_outcome_contract_id"] in selected_contract_ids
        ]
        if overall_verdict == "accept" and any(
            selected_review["verdict"] != "sufficient"
            or selected_review["problem_coverage"] != "full"
            or selected_review["proves_intended_operation"] is not True
            or bool(selected_review["residual_untested_paths"])
            for selected_review in selected_reviews
        ):
            raise ValueError("falsification_accepts_insufficient_outcome_semantics")
        selected_contract_id = selected_contract_ids[0] if len(selected_contract_ids) == 1 else None

    bound = dict(review)
    bound.pop("adversarial_evidence_receipt", None)
    bound["selected_positive_outcome_contract_id"] = selected_contract_id
    bound["selected_positive_outcome_contract_ids"] = selected_contract_ids
    bound["outcome_contract_reviews"] = contract_reviews
    evidence_refs_raw = review.get("evidence_refs")
    if not isinstance(evidence_refs_raw, list) or not evidence_refs_raw:
        raise ValueError("falsification_verified_evidence_refs_missing")
    cited_evidence_ids: set[str] = set()
    bound_evidence_refs: list[dict[str, Any]] = []
    for index, evidence_ref in enumerate(evidence_refs_raw):
        if not isinstance(evidence_ref, Mapping):
            raise ValueError(f"falsification_evidence_ref_invalid:{index}")
        evidence_id = _string(evidence_ref.get("ref"))
        finding = _string(evidence_ref.get("finding"))
        evidence = evidence_pool.get(evidence_id or "")
        if evidence_id is None or evidence is None:
            raise ValueError(f"falsification_evidence_ref_unbound:{index}")
        if evidence_id in cited_evidence_ids:
            raise ValueError(f"falsification_evidence_ref_duplicate:{index}")
        if finding is None:
            raise ValueError(f"falsification_evidence_finding_missing:{index}")
        effect = _string(evidence.get("adversarial_effect"))
        if effect not in _FALSIFICATION_EVIDENCE_EFFECTS:
            effect = "supports_selection"
        cited_evidence_ids.add(evidence_id)
        bound_evidence_refs.append(
            {
                **dict(evidence_ref),
                "ref": evidence_id,
                "finding": finding,
                "effect": effect,
            }
        )
    bound["evidence_refs"] = bound_evidence_refs
    if any(
        ref not in cited_evidence_ids
        for contract_review in contract_reviews
        for ref in contract_review["evidence_refs"]
    ):
        raise ValueError("falsification_outcome_contract_evidence_not_cited")

    dispositions_raw = review.get("material_risk_dispositions")
    if not isinstance(dispositions_raw, list):
        raise ValueError("falsification_risk_dispositions_invalid")
    bound_dispositions: list[dict[str, Any]] = []
    for index, disposition in enumerate(dispositions_raw):
        if not isinstance(disposition, Mapping):
            raise ValueError(f"falsification_risk_disposition_invalid:{index}")
        refs = _string_list(disposition.get("evidence_refs"), nonempty=True)
        if refs is None or any(ref not in cited_evidence_ids for ref in refs):
            raise ValueError(f"falsification_risk_evidence_ref_unbound:{index}")
        bound_dispositions.append({**dict(disposition), "evidence_refs": refs})
    bound["material_risk_dispositions"] = bound_dispositions

    evidence_projection = [
        {
            "mechanism_evidence_id": evidence_id,
            "mechanism_evidence_sha256": _canonical_sha256(evidence_pool[evidence_id]),
            "evidence_type": evidence_pool[evidence_id]["evidence_type"],
        }
        for evidence_id in sorted(cited_evidence_ids)
    ]
    receipt: dict[str, Any] = {
        "schema_version": 3,
        "producer": "backlog_core.bind_falsification_review",
        "binding_method": "runner_causal_falsification_binding_v1",
        "problem_id": problem_id,
        "selected_option_id": selected_option_id,
        "selected_option_sha256": _canonical_sha256(selected_option),
        "research_receipt_sha256": research_receipt_sha256,
        "review_claims_sha256": _canonical_sha256(bound),
        "evidence": evidence_projection,
        "falsification_attempts": verified_attempts,
        "deterministic_mechanism_closures": verified_closures,
        "selected_positive_outcome_contract_id": selected_contract_id,
        "selected_positive_outcome_contract_ids": selected_contract_ids,
        "outcome_contract_reviews_sha256": _canonical_sha256(contract_reviews),
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    bound["adversarial_evidence_receipt"] = receipt
    return bound


def falsification_review_receipt_errors(
    review: Mapping[str, Any],
    *,
    problem_id: str,
    selected_option: Mapping[str, Any],
    research: Mapping[str, Any] | None,
) -> list[str]:
    """Recompute the server binding and reject any review, option, or receipt drift."""

    receipt = review.get("adversarial_evidence_receipt")
    if not isinstance(receipt, Mapping):
        return ["selection_falsification_server_receipt_missing"]
    claims = dict(review)
    claims.pop("adversarial_evidence_receipt", None)
    try:
        rebound = bind_falsification_review(
            claims,
            problem_id=problem_id,
            selected_option=selected_option,
            research=research,
        )
    except ValueError as exc:
        return [f"selection_falsification_server_binding_invalid:{exc}"]
    errors: list[str] = []
    if claims != {
        key: value for key, value in rebound.items() if key != "adversarial_evidence_receipt"
    }:
        errors.append("selection_falsification_server_derived_claims_changed")
    if dict(receipt) != rebound.get("adversarial_evidence_receipt"):
        errors.append("selection_falsification_server_receipt_changed")
    return errors


def _verification_only_target(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    parts = [part for part in normalized.split("/") if part]
    leaf = parts[-1] if parts else ""
    return (
        any(part in {"test", "tests", "__tests__"} for part in parts)
        or leaf.startswith("test_")
        or leaf.endswith("_test.py")
        or ".test." in leaf
        or ".spec." in leaf
    )


def research_evidence_references(research: Mapping[str, Any] | None) -> set[str]:
    """Return exact evidence identifiers that downstream claims may cite."""

    if not isinstance(research, Mapping):
        return set()
    refs: set[str] = set()
    for member in _research_dossier_members(research):
        for field in ("inspected_files", "inspected_symbols"):
            values = _string_list(member.get(field)) or []
            refs.update(values)
        for artifact in member.get("artifact_refs", []):
            if not isinstance(artifact, Mapping):
                continue
            for field in ("artifact_id", "path"):
                value = _string(artifact.get(field))
                if value is not None:
                    refs.add(value)
        for experiment in member.get("experiments", []):
            if not isinstance(experiment, Mapping):
                continue
            experiment_id = _string(experiment.get("experiment_id"))
            if experiment_id is not None:
                refs.add(experiment_id)
            refs.update(_string_list(experiment.get("artifact_refs")) or [])
    refs.update(_verified_control_receipts(research))
    refs.update(_verified_mechanism_evidence(research))
    refs.update(_verified_failure_paths(research))
    return refs


def _research_evidence_reference_identities(
    research: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Map evidence aliases to the underlying evidence object they identify.

    An artifact's ID and path are two spellings for one receipt, not independent
    support.  Keeping binding aliases separate from evidence identity let a broad option
    cite the ID for one consumer and the path for another.  This projection collapses
    those aliases before independence is evaluated.
    """

    allowed_refs = research_evidence_references(research)
    identities = {ref: f"reference:{ref}" for ref in allowed_refs}
    if not isinstance(research, Mapping):
        return identities

    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if left_root < right_root:
            parent[right_root] = left_root
        else:
            parent[left_root] = right_root

    for artifact in research.get("artifact_refs", []):
        if not isinstance(artifact, Mapping):
            continue
        aliases = [
            value
            for field in ("artifact_id", "path")
            for value in [_string(artifact.get(field))]
            if value is not None
        ]
        if not aliases:
            continue
        for alias in aliases:
            find(alias)
        for alias in aliases[1:]:
            union(aliases[0], alias)

    for experiment in research.get("experiments", []):
        if not isinstance(experiment, Mapping):
            continue
        experiment_id = _string(experiment.get("experiment_id"))
        artifact_refs = _string_list(experiment.get("artifact_refs")) or []
        if experiment_id is None:
            continue
        find(experiment_id)
        for artifact_ref in artifact_refs:
            union(experiment_id, artifact_ref)

    inspected_files = _string_list(research.get("inspected_files")) or []
    inspected_symbols = _string_list(research.get("inspected_symbols")) or []
    for symbol in inspected_symbols:
        # Current proof records use ``path:symbol`` when a symbol carries a file
        # qualifier.  Prefer the longest exact file prefix and leave unqualified
        # names independent rather than guessing module-to-path resolution.
        matching_files = [path for path in inspected_files if symbol.startswith(f"{path}:")]
        if matching_files:
            union(symbol, max(matching_files, key=len))

    components: dict[str, list[str]] = {}
    for alias in parent:
        components.setdefault(find(alias), []).append(alias)
    for aliases in components.values():
        identity = "artifact:" + min(aliases)
        for alias in aliases:
            identities[alias] = identity
    return identities


def research_limitation_references(research: Mapping[str, Any] | None) -> set[str]:
    """Return material boundary/unknown identifiers a plan limitation may cite."""

    if not isinstance(research, Mapping):
        return set()
    refs = set(_string_list(research.get("evidence_boundaries")) or [])
    for unknown in research.get("material_unknowns", []):
        if not isinstance(unknown, Mapping):
            continue
        for field in ("unknown_id", "unknown"):
            value = _string(unknown.get(field))
            if value is not None:
                refs.add(value)
    return refs


def _research_hypotheses(
    research: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(research, Mapping):
        return {}
    raw = research.get("root_cause_hypotheses")
    return {
        hypothesis_id: hypothesis
        for hypothesis in (raw if isinstance(raw, list) else [])
        if isinstance(hypothesis, Mapping)
        for hypothesis_id in [_string(hypothesis.get("hypothesis_id"))]
        if hypothesis_id is not None
    }


def _verified_symbol_paths(
    research: Mapping[str, Any] | None,
) -> dict[str, str]:
    if not isinstance(research, Mapping):
        return {}
    verification = research.get("evidence_verification")
    if not isinstance(verification, Mapping) or verification.get("status") != "verified":
        return {}
    raw = verification.get("inspected_symbols")
    return {
        symbol: path
        for receipt in (raw if isinstance(raw, list) else [])
        if isinstance(receipt, Mapping)
        for symbol in [_string(receipt.get("symbol"))]
        for path in [_string(receipt.get("path"))]
        if symbol is not None and path is not None
    }


def _create_target_integration_reasons(
    target: Mapping[str, Any],
    *,
    index: int,
    verified_symbol_paths: Mapping[str, str],
    verified_evidence: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Validate the existing boundary that makes a new production target actionable.

    A file that does not exist at the researched revision cannot itself have been read or
    attested. Requiring that would make every legitimate ``action:create`` plan
    impossible. Instead, creation is grounded through an existing, runner-inspected
    integration point on the demonstrated causal path.
    """

    reasons: list[str] = []
    binding = target.get("integration_binding")
    if not isinstance(binding, Mapping):
        return [f"change_plan_create_target_integration_binding_missing:{index}"]

    path = _string(binding.get("path"))
    symbol = _string(binding.get("symbol"))
    relationship = _string(binding.get("relationship"))
    refs = _string_list(binding.get("evidence_refs"), nonempty=True)
    if path is None or symbol is None:
        reasons.append(f"change_plan_create_target_integration_point_invalid:{index}")
    elif verified_symbol_paths.get(symbol) != path:
        reasons.append(f"change_plan_create_target_integration_point_unverified:{index}")
    if relationship is None:
        reasons.append(f"change_plan_create_target_integration_relationship_missing:{index}")
    if refs is None:
        reasons.append(f"change_plan_create_target_integration_evidence_missing:{index}")
        return reasons

    target_refs = _string_list(target.get("evidence_refs"), nonempty=True)
    if target_refs is None or set(target_refs) != set(refs):
        reasons.append(f"change_plan_create_target_integration_evidence_mismatch:{index}")

    for ref in refs:
        evidence = verified_evidence.get(ref)
        if evidence is None:
            reasons.append(f"change_plan_create_target_integration_evidence_unbound:{index}:{ref}")
            continue
        code_paths = evidence.get("code_paths")
        if (
            path is not None
            and symbol is not None
            and not any(
                isinstance(point, Mapping)
                and _string(point.get("path")) == path
                and _string(point.get("symbol")) == symbol
                for point in (code_paths if isinstance(code_paths, list) else [])
            )
        ):
            reasons.append(
                f"change_plan_create_target_integration_evidence_not_on_path:{index}:{ref}"
            )
    return reasons


def _research_binding_reasons(
    coverage: Mapping[str, Any],
    *,
    research: Mapping[str, Any] | None,
) -> list[str]:
    """Bind an option's intervention to one verified stage-3 mechanism."""

    reasons: list[str] = []
    binding = coverage.get("research_binding")
    if not isinstance(binding, Mapping):
        return ["solution_option_research_binding_missing"]

    hypothesis_id = _string(binding.get("hypothesis_id"))
    hypothesis_statement = _string(binding.get("hypothesis_statement"))
    mechanism_symbols = _string_list(binding.get("mechanism_symbols"), nonempty=True)
    supporting_refs = _string_list(binding.get("supporting_evidence_refs"), nonempty=True)
    counter_refs = _string_list(binding.get("counterevidence_refs"))
    attempt_refs = _string_list(binding.get("falsification_attempt_refs"))
    closure_refs = _string_list(binding.get("deterministic_closure_refs"))
    intervention_points_raw = binding.get("intervention_points")
    intervention_points = (
        intervention_points_raw if isinstance(intervention_points_raw, list) else []
    )
    if hypothesis_id is None:
        reasons.append("solution_option_research_hypothesis_id_missing")
    if hypothesis_statement is None:
        reasons.append("solution_option_research_hypothesis_statement_missing")
    if mechanism_symbols is None:
        reasons.append("solution_option_research_mechanism_symbols_invalid")
    if supporting_refs is None:
        reasons.append("solution_option_research_supporting_refs_invalid")
    if counter_refs is None:
        reasons.append("solution_option_research_counterevidence_refs_invalid")
    if attempt_refs is None:
        reasons.append("solution_option_research_falsification_attempt_refs_invalid")
    if closure_refs is None:
        reasons.append("solution_option_research_deterministic_closure_refs_invalid")
    if not intervention_points:
        reasons.append("solution_option_intervention_points_missing")

    hypothesis = _research_hypotheses(research).get(hypothesis_id or "")
    if hypothesis is None:
        reasons.append("solution_option_research_hypothesis_unbound")
        return reasons
    expected_statement = _string(hypothesis.get("statement"))
    expected_symbols = _string_list(hypothesis.get("mechanism_symbols"), nonempty=True)
    expected_support = _string_list(hypothesis.get("supporting_evidence"), nonempty=True)
    expected_counter = _string_list(hypothesis.get("counterevidence"))
    attempts_raw = hypothesis.get("falsification_attempts")
    expected_attempt_refs = [
        attempt_id
        for attempt in (attempts_raw if isinstance(attempts_raw, list) else [])
        if isinstance(attempt, Mapping)
        for attempt_id in [_string(attempt.get("attempt_id"))]
        if attempt_id is not None
    ]
    expected_closures = verified_deterministic_mechanism_closures(
        dict(research) if isinstance(research, Mapping) else None,
        hypothesis_id=hypothesis_id or "",
    )
    expected_closure_refs = sorted(
        closure_id
        for closure in expected_closures
        if isinstance(closure, Mapping)
        for closure_id in [_string(closure.get("closure_receipt_id"))]
        if closure_id is not None
    )
    if hypothesis_statement != expected_statement:
        reasons.append("solution_option_research_hypothesis_statement_mismatch")
    if mechanism_symbols != expected_symbols:
        reasons.append("solution_option_research_mechanism_symbols_mismatch")
    if supporting_refs != expected_support:
        reasons.append("solution_option_research_supporting_refs_mismatch")
    if counter_refs != expected_counter:
        reasons.append("solution_option_research_counterevidence_refs_mismatch")
    if attempt_refs != expected_attempt_refs:
        reasons.append("solution_option_research_falsification_attempt_refs_mismatch")
    if closure_refs != expected_closure_refs:
        reasons.append("solution_option_research_deterministic_closure_refs_mismatch")
    if bool(expected_attempt_refs) == bool(expected_closure_refs):
        reasons.append("solution_option_research_proof_route_invalid")
    elif expected_attempt_refs:
        verified_attempts = _verified_selected_falsification_attempts(
            research,
            hypothesis_id=hypothesis_id or "",
            attempt_refs=attempt_refs or [],
        )
        if len(verified_attempts) != len(expected_attempt_refs):
            reasons.append("solution_option_research_falsification_attempts_unverified")
        if not any(attempt.get("outcome") == "survived" for attempt in verified_attempts):
            reasons.append("solution_option_research_hypothesis_not_falsification_survived")
        if any(attempt.get("outcome") == "disproved" for attempt in verified_attempts):
            reasons.append("solution_option_research_hypothesis_falsification_disproved")
    else:
        verified_closures = _verified_selected_deterministic_closures(
            research,
            hypothesis_id=hypothesis_id or "",
            closure_refs=closure_refs or [],
        )
        if len(verified_closures) != len(expected_closure_refs):
            reasons.append("solution_option_research_deterministic_closure_unverified")

    verified_symbol_paths = _verified_symbol_paths(research)
    sufficient_control_points = 0
    intervention_targets: dict[tuple[str, str], str] = {}
    for index, point in enumerate(intervention_points):
        if not isinstance(point, Mapping):
            reasons.append(f"solution_option_intervention_point_invalid:{index}")
            continue
        mechanism_symbol = _string(point.get("mechanism_symbol"))
        target_symbol = _string(point.get("target_symbol"))
        target_path = _string(point.get("target_path"))
        intervention = _string(point.get("intervention"))
        if mechanism_symbol is None or mechanism_symbol not in (expected_symbols or []):
            reasons.append(f"solution_option_intervention_mechanism_symbol_unbound:{index}")
        controlled_symbols = _string_list(
            point.get("controls_mechanism_symbols"),
            nonempty=True,
        )
        if controlled_symbols is None:
            # Preserve the single-symbol v1 shape while requiring multi-symbol
            # mechanisms to state exactly which chain a control point dominates.
            controlled_symbols = [mechanism_symbol] if mechanism_symbol is not None else []
        if (
            not controlled_symbols
            or len(controlled_symbols) != len(set(controlled_symbols))
            or any(symbol not in (expected_symbols or []) for symbol in controlled_symbols)
            or mechanism_symbol not in controlled_symbols
        ):
            reasons.append(f"solution_option_intervention_controlled_symbols_invalid:{index}")
        causal_role = _string(point.get("causal_role"))
        if causal_role is None and len(expected_symbols or []) == 1:
            causal_role = "sufficient_control_point"
        if causal_role not in {"sufficient_control_point", "supporting_change"}:
            reasons.append(f"solution_option_intervention_causal_role_invalid:{index}")
        elif causal_role == "sufficient_control_point":
            if set(controlled_symbols) != set(expected_symbols or []):
                reasons.append(f"solution_option_intervention_control_point_not_sufficient:{index}")
            elif (
                _string(point.get("sufficiency_rationale")) is None
                and len(expected_symbols or []) > 1
            ):
                reasons.append(
                    f"solution_option_intervention_sufficiency_rationale_missing:{index}"
                )
            else:
                sufficient_control_points += 1
        if target_symbol is None or target_path is None:
            reasons.append(f"solution_option_intervention_target_invalid:{index}")
        elif verified_symbol_paths.get(target_symbol) != target_path:
            reasons.append(f"solution_option_intervention_target_unverified:{index}")
        elif target_symbol != mechanism_symbol:
            reasons.append(f"solution_option_intervention_target_mechanism_mismatch:{index}")
        if intervention is None:
            reasons.append(f"solution_option_intervention_effect_missing:{index}")
        if target_path is not None and target_symbol is not None and intervention is not None:
            target_key = (target_path, target_symbol)
            previous = intervention_targets.get(target_key)
            if previous is not None:
                reasons.append(f"solution_option_intervention_target_duplicate:{index}")
                if previous != intervention:
                    reasons.append(f"solution_option_intervention_target_conflict:{index}")
            else:
                intervention_targets[target_key] = intervention
    if expected_symbols is not None and sufficient_control_points == 0:
        reasons.append("solution_option_causally_sufficient_intervention_missing")
    return reasons


def plan_revision_id_for(plan: Mapping[str, Any]) -> str:
    """Return the server-owned content address for a plan's semantic payload."""

    payload = {
        "schema_version": 1,
        "plan": {field: plan.get(field) for field in _PLAN_REVISION_FIELDS},
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"planrev:sha256:{hashlib.sha256(canonical).hexdigest()}"


def assign_plan_revision_id(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Copy *plan* and overwrite any model-supplied revision with the server value."""

    assigned = dict(plan)
    assigned["plan_revision_id"] = plan_revision_id_for(assigned)
    assigned["plan_revision_source"] = "server_content_addressed_v1"
    return assigned


def infer_live_verification_requirement(
    problem: Mapping[str, Any] | None,
    research: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Infer whether resolution needs post-change runtime evidence.

    Replaying pure code is not, by itself, a reason to demand a later live rollout.
    Live proof is reserved for an actual platform/service/external runtime boundary.
    """

    reasons: list[str] = []
    research_map = research if isinstance(research, Mapping) else {}
    problem_map = problem if isinstance(problem, Mapping) else {}
    research_members = _research_dossier_members(research_map)
    verified_mechanism_evidence = list(_verified_mechanism_evidence(research_map).values())
    verified_experiments = [
        experiment
        for member in research_members
        for verification in [member.get("evidence_verification")]
        if isinstance(verification, Mapping) and verification.get("status") == "verified"
        for experiment in (
            verification.get("experiments")
            if isinstance(verification.get("experiments"), list)
            else []
        )
        if isinstance(experiment, Mapping)
    ]
    verified_experiment_ids = {
        experiment_id
        for evidence in verified_mechanism_evidence
        for experiment_id in (_string_list(evidence.get("experiment_ids")) or [])
    }
    verified_experiment_ids.update(
        experiment_id
        for experiment in verified_experiments
        for experiment_id in [_string(experiment.get("experiment_id"))]
        if experiment_id is not None
    )
    declared_verified_experiments = [
        experiment
        for member in research_members
        for experiment in (
            member.get("experiments") if isinstance(member.get("experiments"), list) else []
        )
        if isinstance(experiment, Mapping)
        and _string(experiment.get("experiment_id")) in verified_experiment_ids
    ]
    for experiment in [*verified_experiments, *declared_verified_experiments]:
        experiment_id = _string(experiment.get("experiment_id"))
        if experiment_id is None or experiment_id not in verified_experiment_ids:
            continue
        if experiment.get("scenario_kind") == "live_runtime":
            reasons.append("research_verified_live_runtime_boundary")
        platform = _string(experiment.get("platform_requirement"))
        if platform is not None and platform != "any":
            reasons.append(f"research_requires_platform:{platform}")
    for evidence in verified_mechanism_evidence:
        if evidence.get("evidence_type") == "live_runtime":
            reasons.append("research_mechanism_evidence_live_runtime")
        platform = _string(evidence.get("platform_requirement"))
        if platform is not None and platform != "any":
            reasons.append(f"research_mechanism_evidence_requires_platform:{platform}")
    # A runner directory, exit code, normalized-event stream, or report is evidence
    # transport for every research method. It is not by itself provenance that the
    # originating problem crosses a runtime boundary.
    for artifact in (
        artifact
        for member in research_members
        for artifact in (
            member.get("artifact_refs") if isinstance(member.get("artifact_refs"), list) else []
        )
    ):
        if not isinstance(artifact, Mapping):
            continue
        artifact_id = (_string(artifact.get("artifact_id")) or "").casefold()
        if artifact_id.startswith("runner:"):
            continue
        kind = (_string(artifact.get("kind")) or "").casefold()
        if kind in _RUNTIME_ARTIFACT_KINDS:
            reasons.append(f"runtime_artifact:{kind}")
    narrative = " ".join(
        _string(problem_map.get(field)) or ""
        for field in ("title", "problem", "user_impact", "evidence_summary")
    ).casefold()
    if any(marker in narrative for marker in _RUNTIME_MARKERS) or _RUNTIME_BOUNDARY_RE.search(
        narrative
    ):
        reasons.append("problem_narrative_identifies_runtime_boundary")
    if _EXTERNAL_PROVIDER_RUNTIME_RE.search(narrative):
        reasons.append("problem_narrative_identifies_external_provider_boundary")
    return bool(reasons), list(dict.fromkeys(reasons))


def assess_solution_option_readiness(
    option: Mapping[str, Any] | None,
    *,
    research: Mapping[str, Any] | None = None,
) -> tuple[bool, list[str]]:
    """Validate causal coverage, scope evidence, and evidence binding for one option."""

    if not isinstance(option, Mapping):
        return False, ["solution_option_missing"]
    reasons: list[str] = []
    if _string(option.get("_parse_warning")) is not None:
        reasons.append("solution_option_parse_warning_present")
    option_id = _string(option.get("option_id"))
    for field in (
        "option_id",
        "problem_id",
        "family_id",
        "summary",
        "tradeoffs",
        "recurrence_prevention",
        "change_surface_hypothesis",
        "test_implications",
        "rationale",
    ):
        if _string(option.get(field)) is None:
            reasons.append(f"solution_option_invalid_{field}")

    coverage = option.get("causal_coverage")
    if not isinstance(coverage, Mapping):
        reasons.append("solution_option_causal_coverage_missing")
    else:
        if _string(coverage.get("mechanism_addressed")) is None:
            reasons.append("solution_option_mechanism_missing")
        if _string_list(coverage.get("symptoms_covered"), nonempty=True) is None:
            reasons.append("solution_option_symptoms_covered_invalid")
        for field in (
            "unsupported_assumptions",
            "residual_recurrence_paths",
            "compatibility_risks",
        ):
            if _string_list(coverage.get(field)) is None:
                reasons.append(f"solution_option_{field}_invalid")
        testability = coverage.get("testability")
        if not isinstance(testability, Mapping):
            reasons.append("solution_option_testability_missing")
        else:
            for field in ("before", "after"):
                if _string(testability.get(field)) is None:
                    reasons.append(f"solution_option_testability_{field}_missing")
        reasons.extend(_research_binding_reasons(coverage, research=research))

    verified_paths = _verified_failure_paths(research)
    research_binding = coverage.get("research_binding") if isinstance(coverage, Mapping) else None
    bound_hypothesis_id = (
        _string(research_binding.get("hypothesis_id"))
        if isinstance(research_binding, Mapping)
        else None
    )
    bound_mechanism_symbols = (
        _string_list(research_binding.get("mechanism_symbols"), nonempty=True)
        if isinstance(research_binding, Mapping)
        else None
    )
    scope = option.get("scope_evidence")
    if not isinstance(scope, Mapping):
        reasons.append("solution_option_scope_evidence_missing")
    else:
        scope_level = _string(scope.get("scope_level"))
        if scope_level not in _SCOPE_LEVELS:
            reasons.append("solution_option_scope_level_invalid")
        paths = scope.get("independent_consumers_or_failure_paths")
        if not isinstance(paths, list) or not paths:
            reasons.append("solution_option_scope_paths_missing")
            paths = []
        bound_path_receipts: list[Mapping[str, Any]] = []
        cited_path_ids: set[str] = set()
        for index, path in enumerate(paths):
            if not isinstance(path, Mapping):
                reasons.append(f"solution_option_scope_path_invalid:{index}")
                continue
            name = _string(path.get("name"))
            refs = _string_list(path.get("evidence_refs"), nonempty=True)
            if name is None:
                reasons.append(f"solution_option_scope_path_name_missing:{index}")
            if refs is None or len(refs) != 1:
                reasons.append(f"solution_option_scope_refs_invalid:{index}")
                continue
            path_id = refs[0]
            path_receipt = verified_paths.get(path_id)
            if path_receipt is None:
                reasons.append(f"solution_option_scope_path_receipt_unbound:{index}:{path_id}")
                continue
            if path_id in cited_path_ids:
                reasons.append(f"solution_option_scope_path_receipt_duplicate:{index}")
            cited_path_ids.add(path_id)
            if name != _string(path_receipt.get("path_name")):
                reasons.append(f"solution_option_scope_path_name_mismatch:{index}")
            if _string(path_receipt.get("hypothesis_id")) != bound_hypothesis_id:
                reasons.append(f"solution_option_scope_path_hypothesis_mismatch:{index}")
            if (
                _string_list(path_receipt.get("mechanism_symbols"), nonempty=True)
                != bound_mechanism_symbols
            ):
                reasons.append(f"solution_option_scope_path_mechanism_mismatch:{index}")
            bound_path_receipts.append(path_receipt)
        if isinstance(coverage, Mapping):
            reasons.extend(
                _intervention_sufficiency_reasons(
                    coverage,
                    research=research,
                    bound_scope_paths=bound_path_receipts,
                )
            )
        if scope_level in {"multiple_independent_paths", "shared_abstraction"}:
            if len(bound_path_receipts) < 2 or len(cited_path_ids) < 2:
                reasons.append("solution_option_broad_scope_requires_two_paths")
            else:
                consumer_kinds = [
                    _string(path.get("consumer_identity", {}).get("kind"))
                    for path in bound_path_receipts
                    if isinstance(path.get("consumer_identity"), Mapping)
                ]
                if len(consumer_kinds) != len(bound_path_receipts) or any(
                    kind not in _BREADTH_CONSUMER_KINDS for kind in consumer_kinds
                ):
                    reasons.append("solution_option_broad_scope_requires_production_consumers")
                independence_keys = {
                    _string(path.get("independence_key")) for path in bound_path_receipts
                }
                if None in independence_keys or len(independence_keys) != len(bound_path_receipts):
                    reasons.append("solution_option_broad_scope_requires_independent_failure_paths")
                # One originating run can expose multiple independent consumers
                # or paths. Independence is established by the runner's path key,
                # not by forcing artificially disjoint atom sets.
        class_claim_text = " ".join(
            _string(option.get(field)) or ""
            for field in ("summary", "rationale", "recurrence_prevention")
        )
        if _CLASS_SCOPE_RE.search(class_claim_text) and scope_level == "single_path":
            reasons.append("solution_option_class_claim_lacks_broad_scope_evidence")
    if option_id is None:
        reasons.append("solution_option_identity_missing")
    return not reasons, reasons


def assess_selection_readiness(
    selection: Mapping[str, Any] | None,
    *,
    options: Sequence[Mapping[str, Any]],
    research: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Validate selection linkage, causal evaluation, falsification, and labeling."""

    if not isinstance(selection, Mapping):
        return False, ["selection_missing"]
    reasons: list[str] = []
    if _string(selection.get("_parse_warning")) is not None:
        reasons.append("selection_parse_warning_present")
    for field in (
        "problem_id",
        "selected_option_id",
        "selected_family_id",
        "selection_rationale",
        "repo_intent_alignment",
        "why_other_options_were_not_selected",
    ):
        if _string(selection.get(field)) is None:
            reasons.append(f"selection_{field}_missing")
    if not isinstance(selection.get("needs_ux_review"), bool):
        reasons.append("selection_needs_ux_review_invalid")
    selected_option_id = _string(selection.get("selected_option_id"))
    selected_family_id = _string(selection.get("selected_family_id"))
    selected_option = next(
        (option for option in options if _string(option.get("option_id")) == selected_option_id),
        None,
    )
    option_ready, option_reasons = assess_solution_option_readiness(
        selected_option, research=research
    )
    if not option_ready:
        reasons.extend(option_reasons)
    if isinstance(selected_option, Mapping) and (
        _string(selected_option.get("family_id")) != selected_family_id
    ):
        reasons.append("selection_family_mismatch")
    if isinstance(selected_option, Mapping) and (
        _string(selected_option.get("problem_id")) != _string(selection.get("problem_id"))
    ):
        reasons.append("selection_problem_mismatch")

    evaluation = selection.get("causal_coverage_evaluation")
    if not isinstance(evaluation, Mapping):
        reasons.append("selection_causal_evaluation_missing")
    else:
        if _string(evaluation.get("mechanism_fit")) is None:
            reasons.append("selection_mechanism_fit_missing")
        for field in ("accepted_unsupported_assumptions", "accepted_residual_risks"):
            if _string_list(evaluation.get(field)) is None:
                reasons.append(f"selection_{field}_invalid")
        class_level_sufficient = evaluation.get("class_level_evidence_sufficient")
        if not isinstance(class_level_sufficient, bool):
            reasons.append("selection_class_level_evidence_decision_missing")
        selected_scope_raw = (
            selected_option.get("scope_evidence") if isinstance(selected_option, Mapping) else None
        )
        selected_scope = selected_scope_raw if isinstance(selected_scope_raw, Mapping) else {}
        if (
            _string(selected_scope.get("scope_level"))
            in {"multiple_independent_paths", "shared_abstraction"}
            and class_level_sufficient is not True
        ):
            reasons.append("selection_broad_scope_without_class_level_evidence")

    falsification = selection.get("falsification_review")
    if not isinstance(falsification, Mapping):
        reasons.append("selection_falsification_missing")
    else:
        if falsification.get("verdict") != "accept":
            reasons.append("selection_falsification_not_accepted")
        if _string(falsification.get("problem_id")) != _string(selection.get("problem_id")):
            reasons.append("selection_falsification_problem_mismatch")
        if _string(falsification.get("selected_option_id")) != selected_option_id:
            reasons.append("selection_falsification_option_mismatch")
        for field in ("strongest_counterargument", "evidence_that_would_change_verdict"):
            if _string(falsification.get(field)) is None:
                reasons.append(f"selection_falsification_{field}_missing")
        for field in ("unsupported_assumptions", "residual_risks"):
            if _string_list(falsification.get(field)) is None:
                reasons.append(f"selection_falsification_{field}_invalid")
        if isinstance(selected_option, Mapping):
            reasons.extend(
                falsification_review_receipt_errors(
                    falsification,
                    problem_id=_string(selection.get("problem_id")) or "",
                    selected_option=selected_option,
                    research=research,
                )
            )
        allowed_evidence = set(_verified_mechanism_evidence(research))
        evidence_refs = falsification.get("evidence_refs")
        adversarial_refs: set[str] = set()
        if not isinstance(evidence_refs, list) or not evidence_refs:
            reasons.append("selection_falsification_evidence_refs_missing")
        else:
            for index, evidence_ref in enumerate(evidence_refs):
                if not isinstance(evidence_ref, Mapping):
                    reasons.append(f"selection_falsification_evidence_ref_invalid:{index}")
                    continue
                ref = _string(evidence_ref.get("ref"))
                if ref is None or ref not in allowed_evidence:
                    reasons.append(
                        f"selection_falsification_evidence_ref_unbound:{index}:{ref or ''}"
                    )
                if _string(evidence_ref.get("finding")) is None:
                    reasons.append(f"selection_falsification_evidence_finding_missing:{index}")
                if evidence_ref.get("effect") not in _FALSIFICATION_EVIDENCE_EFFECTS:
                    reasons.append(f"selection_falsification_evidence_effect_invalid:{index}")
                elif (
                    evidence_ref.get("effect")
                    in {
                        "challenges_selection",
                        "limits_scope",
                    }
                    and ref is not None
                ):
                    adversarial_refs.add(ref)
        has_adversarial_basis = falsification_acceptance_has_adversarial_basis(falsification)
        if falsification.get("verdict") == "accept" and not has_adversarial_basis:
            reasons.append("selection_falsification_accept_without_adversarial_evidence")
        critical_findings = falsification.get("critical_findings")
        if not isinstance(critical_findings, list):
            reasons.append("selection_falsification_critical_findings_invalid")
        else:
            for index, finding in enumerate(critical_findings):
                if (
                    not isinstance(finding, Mapping)
                    or _string(finding.get("finding")) is None
                    or finding.get("affects") not in {"root_cause", "interface", "change_surface"}
                    or _string_list(finding.get("evidence_refs"), nonempty=True) is None
                    or any(
                        ref not in allowed_evidence
                        for ref in (_string_list(finding.get("evidence_refs"), nonempty=True) or [])
                    )
                ):
                    reasons.append(f"selection_falsification_critical_finding_invalid:{index}")
            if falsification.get("verdict") == "accept" and critical_findings:
                reasons.append("selection_falsification_accepts_critical_finding")

        material_risks: set[str] = set()
        if isinstance(selected_option, Mapping):
            coverage = selected_option.get("causal_coverage")
            if isinstance(coverage, Mapping):
                for field in (
                    "unsupported_assumptions",
                    "residual_recurrence_paths",
                    "compatibility_risks",
                ):
                    material_risks.update(_string_list(coverage.get(field)) or [])
        for field in ("unsupported_assumptions", "residual_risks"):
            material_risks.update(_string_list(falsification.get(field)) or [])
        dispositions = falsification.get("material_risk_dispositions")
        if not isinstance(dispositions, list):
            reasons.append("selection_falsification_risk_dispositions_invalid")
        else:
            disposed_risks: set[str] = set()
            for index, disposition in enumerate(dispositions):
                if not isinstance(disposition, Mapping):
                    reasons.append(f"selection_falsification_risk_disposition_invalid:{index}")
                    continue
                risk = _string(disposition.get("risk"))
                decision = disposition.get("disposition")
                if risk is None or risk not in material_risks:
                    reasons.append(f"selection_falsification_risk_unbound:{index}:{risk or ''}")
                else:
                    if risk in disposed_risks:
                        reasons.append(
                            f"selection_falsification_risk_disposition_duplicate:{index}"
                        )
                    disposed_risks.add(risk)
                if decision not in _MATERIAL_RISK_DISPOSITIONS:
                    reasons.append(f"selection_falsification_risk_disposition_unknown:{index}")
                if decision == "blocks_selection" and falsification.get("verdict") == "accept":
                    reasons.append("selection_falsification_blocking_risk_accepted")
                refs = _string_list(disposition.get("evidence_refs"), nonempty=True)
                if refs is None or any(ref not in allowed_evidence for ref in refs):
                    reasons.append(f"selection_falsification_risk_evidence_unbound:{index}")
                elif decision == "mitigated" and not (
                    adversarial_refs.intersection(refs) or has_adversarial_basis
                ):
                    reasons.append(
                        f"selection_falsification_mitigation_lacks_adversarial_evidence:{index}"
                    )
                if _string(disposition.get("rationale")) is None:
                    reasons.append(f"selection_falsification_risk_rationale_missing:{index}")
            missing_risks = sorted(material_risks - disposed_risks)
            if missing_risks:
                reasons.append(
                    "selection_falsification_material_risks_undisposed:" + ",".join(missing_risks)
                )

    change_surface = selection.get("change_surface")
    if not isinstance(change_surface, Mapping):
        reasons.append("selection_change_surface_missing")
    else:
        if not isinstance(change_surface.get("user_visible"), bool):
            reasons.append("selection_change_surface_visibility_invalid")
        if _string_list(change_surface.get("kinds"), nonempty=True) is None:
            reasons.append("selection_change_surface_kinds_invalid")
    return not reasons, reasons


def assess_change_plan_readiness(
    plan: Mapping[str, Any] | None,
    *,
    problem: Mapping[str, Any] | None,
    research: Mapping[str, Any] | None,
    selection: Mapping[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Validate a plan's depth and its linkage to the selected evidence chain."""

    if not isinstance(plan, Mapping):
        return False, ["change_plan_missing"]
    reasons: list[str] = []
    if _string(plan.get("_parse_warning")) is not None:
        reasons.append("change_plan_parse_warning_present")
    for field in (
        "change_plan_id",
        "plan_revision_id",
        "case_id",
        "problem_id",
        "selected_option_id",
        "title",
        "problem",
        "user_impact",
        "proposed_fix",
        "rollback_notes",
        "suggested_owner",
        "repo_revision",
        "live_verification_rationale",
    ):
        if _string(plan.get(field)) is None:
            reasons.append(f"change_plan_invalid_{field}")
    actual_plan_revision = _string(plan.get("plan_revision_id"))
    expected_plan_revision = plan_revision_id_for(plan)
    if actual_plan_revision != expected_plan_revision:
        reasons.append("change_plan_revision_id_content_mismatch")
    if plan.get("plan_revision_source") != "server_content_addressed_v1":
        reasons.append("change_plan_revision_source_invalid")
    if verified_outcome_oracles(research):
        try:
            rebound_plan = bind_plan_outcome_oracle(
                plan,
                research=research or {},
                selection=selection,
            )
        except ValueError as exc:
            reasons.append(f"change_plan_outcome_oracle_binding_invalid:{exc}")
        else:
            rebound_roles = rebound_plan.get("outcome_verification_roles")
            actual_roles = plan.get("outcome_verification_roles")
            rebound_original = (
                rebound_roles.get("original_scenario")
                if isinstance(rebound_roles, Mapping)
                else None
            )
            actual_original = (
                actual_roles.get("original_scenario") if isinstance(actual_roles, Mapping) else None
            )
            rebound_reproduction = rebound_plan.get("before_after_reproduction")
            actual_reproduction = plan.get("before_after_reproduction")
            if actual_original != rebound_original or any(
                (
                    actual_reproduction.get(field)
                    if isinstance(actual_reproduction, Mapping)
                    else None
                )
                != (
                    rebound_reproduction.get(field)
                    if isinstance(rebound_reproduction, Mapping)
                    else None
                )
                for field in ("outcome_oracle_id", "required_proof_scope")
            ):
                reasons.append("change_plan_outcome_oracle_binding_changed")
    falsification = (
        selection.get("falsification_review") if isinstance(selection, Mapping) else None
    )
    selected_positive_contract_ids = (
        _string_list(
            falsification.get("selected_positive_outcome_contract_ids"),
            nonempty=True,
        )
        if isinstance(falsification, Mapping)
        else None
    )
    if selected_positive_contract_ids is None and isinstance(falsification, Mapping):
        selected_positive_contract_id = _string(
            falsification.get("selected_positive_outcome_contract_id")
        )
        selected_positive_contract_ids = (
            [selected_positive_contract_id] if selected_positive_contract_id is not None else None
        )
    planned_roles = plan.get("outcome_verification_roles")
    planned_original = (
        planned_roles.get("original_scenario") if isinstance(planned_roles, Mapping) else None
    )
    planned_oracle = (
        planned_original.get("oracle") if isinstance(planned_original, Mapping) else None
    )
    planned_contract_ids = {
        _string(contract.get("positive_outcome_contract_id"))
        for contract in (
            planned_oracle.get("positive_outcome_contracts", [])
            if isinstance(planned_oracle, Mapping)
            else []
        )
        if isinstance(contract, Mapping)
    }
    if isinstance(falsification, Mapping):
        planned_selected_ids = (
            _string_list(
                planned_original.get("selected_positive_outcome_contract_ids"),
                nonempty=True,
            )
            if isinstance(planned_original, Mapping)
            else None
        )
        if selected_positive_contract_ids is None:
            reasons.append("change_plan_selected_positive_outcome_contract_missing")
        elif planned_selected_ids != selected_positive_contract_ids:
            reasons.append("change_plan_selected_positive_outcome_contract_binding_changed")
        elif any(value not in planned_contract_ids for value in selected_positive_contract_ids):
            reasons.append("change_plan_outcome_contract_not_falsification_selected")
    for field in (
        "implementation_steps",
        "verification_steps",
        "success_criteria",
        "verification_commands",
    ):
        if _string_list(plan.get(field), nonempty=True) is None:
            reasons.append(f"change_plan_{field}_invalid")
    steps = _string_list(plan.get("implementation_steps")) or []
    if any(_DISCOVERY_FIRST_RE.search(step) for step in steps):
        reasons.append("change_plan_contains_discovery_first_step")

    targets = plan.get("change_targets")
    if not isinstance(targets, list) or not targets:
        reasons.append("change_plan_targets_missing")
    else:
        for index, target in enumerate(targets):
            if not isinstance(target, Mapping):
                reasons.append(f"change_plan_target_invalid:{index}")
                continue
            if _string(target.get("path")) is None or _string(target.get("change")) is None:
                reasons.append(f"change_plan_target_incomplete:{index}")
            if _string(target.get("action")) not in {"modify", "create"}:
                reasons.append(f"change_plan_target_action_invalid:{index}")
            if _string_list(target.get("symbols"), nonempty=True) is None:
                reasons.append(f"change_plan_target_symbols_invalid:{index}")

    reproduction = plan.get("before_after_reproduction")
    if not isinstance(reproduction, Mapping):
        reasons.append("change_plan_reproduction_mapping_missing")
    else:
        if _string(reproduction.get("original_scenario")) is None:
            reasons.append("change_plan_original_scenario_missing")
        limitation = _string(reproduction.get("proof_limitation"))
        if limitation is not None:
            # Every evidence-sufficient dossier already carries a runner-verified
            # original or faithful replay. A model-authored prose boundary cannot
            # waive that proof at planning time.
            reasons.append("change_plan_proof_limitation_not_runner_verified")
            if _string(reproduction.get("alternate_verification")) is None:
                reasons.append("change_plan_alternate_verification_missing")
            else:
                alternate = " ".join(str(reproduction.get("alternate_verification") or "").split())
                verification_commands = _string_list(plan.get("verification_commands")) or []
                if alternate not in {
                    " ".join(command.split()) for command in verification_commands
                }:
                    reasons.append(
                        "change_plan_alternate_verification_not_in_verification_commands"
                    )
            limitation_refs = _string_list(reproduction.get("proof_limitation_refs"), nonempty=True)
            allowed_limitations = research_limitation_references(research)
            if limitation_refs is None:
                reasons.append("change_plan_proof_limitation_refs_missing")
            elif any(ref not in allowed_limitations for ref in limitation_refs):
                reasons.append("change_plan_proof_limitation_refs_unbound")
        else:
            limitation_refs = reproduction.get("proof_limitation_refs")
            if limitation_refs not in (None, []):
                reasons.append("change_plan_proof_limitation_refs_without_limitation")
            research_experiment_id = _string(reproduction.get("research_experiment_id"))
            research_experiments_raw = (
                research.get("experiments") if isinstance(research, Mapping) else None
            )
            research_experiments = (
                research_experiments_raw if isinstance(research_experiments_raw, list) else []
            )
            research_experiment = next(
                (
                    item
                    for item in research_experiments
                    if isinstance(item, Mapping)
                    and _string(item.get("experiment_id")) == research_experiment_id
                ),
                None,
            )
            roles_for_oracle = plan.get("outcome_verification_roles")
            original_for_oracle = (
                roles_for_oracle.get("original_scenario")
                if isinstance(roles_for_oracle, Mapping)
                else None
            )
            bound_oracle = (
                original_for_oracle.get("oracle")
                if isinstance(original_for_oracle, Mapping)
                else None
            )
            config_oracle = (
                isinstance(bound_oracle, Mapping)
                and bound_oracle.get("kind") == "config_state"
                and bound_oracle.get("proof_scope") == "configuration_state"
                and _string(bound_oracle.get("research_experiment_id")) == research_experiment_id
            )
            multi_scenario_oracle = (
                isinstance(bound_oracle, Mapping)
                and bound_oracle.get("kind") == "multi_scenario"
                and bound_oracle.get("proof_scope") == "multi_scenario"
            )
            if research_experiment_id is None or research_experiment is None:
                reasons.append("change_plan_research_experiment_unbound")
            elif research_experiment.get("scenario_kind") == "static_trace":
                if not config_oracle or research_experiment.get("outcome") != "supports":
                    reasons.append("change_plan_static_trace_cannot_prove_behavioral_outcome")
            elif (
                research_experiment.get("scenario_kind")
                not in {"original_replay", "faithful_replay", "live_runtime"}
                or research_experiment.get("outcome") != "supports"
            ):
                reasons.append("change_plan_research_experiment_not_original_support")
            phase_mappings: dict[str, Mapping[str, Any]] = {}
            for phase in ("before_change", "after_change"):
                value = reproduction.get(phase)
                if not isinstance(value, Mapping):
                    reasons.append(f"change_plan_{phase}_missing")
                elif phase == "after_change" and multi_scenario_oracle:
                    scenario_expectations = value.get("scenario_expectations")
                    scenarios = bound_oracle.get("scenarios")
                    if (
                        not isinstance(scenario_expectations, list)
                        or not isinstance(scenarios, list)
                        or len(scenario_expectations) != len(scenarios)
                        or len(scenarios) < 2
                    ):
                        reasons.append("change_plan_after_multi_scenario_expectations_invalid")
                elif (
                    _string(value.get("command")) is None
                    or _string(value.get("expected_result")) is None
                ):
                    reasons.append(f"change_plan_{phase}_incomplete")
                else:
                    phase_mappings[phase] = value
                    expected_exit_code = value.get("expected_exit_code")
                    if isinstance(expected_exit_code, bool) or not isinstance(
                        expected_exit_code, int
                    ):
                        reasons.append(f"change_plan_{phase}_expected_exit_code_missing")
            if multi_scenario_oracle and reproduction.get("expected_outcome_state") not in {
                "resolved",
                "mitigated",
            }:
                reasons.append("change_plan_expected_outcome_state_invalid")
            before = phase_mappings.get("before_change")
            after = phase_mappings.get("after_change")
            if research_experiment is not None and before is not None:
                if " ".join(str(before.get("command") or "").split()) != " ".join(
                    str(research_experiment.get("command") or "").split()
                ):
                    reasons.append("change_plan_before_command_not_research_replay")
                if before.get("expected_exit_code") != research_experiment.get("exit_code"):
                    reasons.append("change_plan_before_exit_not_research_replay")
                if before.get("observable_assertion") != research_experiment.get(
                    "observable_assertion"
                ):
                    reasons.append("change_plan_before_observable_not_research_replay")
            if before is not None and after is not None:
                if " ".join(str(before.get("command") or "").split()) != " ".join(
                    str(after.get("command") or "").split()
                ):
                    reasons.append("change_plan_after_command_not_original_replay")
                verification_commands = _string_list(plan.get("verification_commands")) or []
                if " ".join(str(after.get("command") or "").split()) not in {
                    " ".join(command.split()) for command in verification_commands
                }:
                    reasons.append("change_plan_after_command_not_in_verification_commands")
                expected_outcome_state = reproduction.get("expected_outcome_state")
                if expected_outcome_state not in {"resolved", "mitigated"}:
                    reasons.append("change_plan_expected_outcome_state_invalid")
                if after.get("expected_exit_code") != 0 and expected_outcome_state != "mitigated":
                    reasons.append("change_plan_nonzero_after_requires_mitigated_outcome")
                after_assertions_raw = after.get("observable_assertions")
                after_assertions = (
                    after_assertions_raw if isinstance(after_assertions_raw, list) else []
                )
                if config_oracle:
                    state_expectations = after.get("state_expectations")
                    if not isinstance(state_expectations, list) or not state_expectations:
                        reasons.append("change_plan_after_config_state_expectations_invalid")
                elif not after_assertions or any(
                    not isinstance(assertion, Mapping)
                    or _observable_assertion_predicate(assertion) is None
                    for assertion in after_assertions
                ):
                    reasons.append("change_plan_after_observable_oracles_invalid")
                baseline_assertion = (
                    research_experiment.get("observable_assertion")
                    if isinstance(research_experiment, Mapping)
                    and isinstance(research_experiment.get("observable_assertion"), Mapping)
                    else {}
                )
                if not config_oracle and not any(
                    isinstance(assertion, Mapping)
                    and _oracle_inverts_baseline(
                        baseline_assertion,
                        assertion,
                    )
                    for assertion in after_assertions
                ):
                    reasons.append("change_plan_after_oracle_does_not_reverse_original_symptom")
                if (
                    expected_outcome_state == "mitigated"
                    and baseline_assertion.get("source") == "exit_code"
                    and after.get("expected_exit_code") != 0
                ):
                    reasons.append("change_plan_mitigation_requires_non_exit_problem_oracle")

    verification_commands_for_roles = _string_list(plan.get("verification_commands")) or []
    reasons.extend(
        _outcome_role_contract_errors(
            plan.get("outcome_verification_roles"),
            verification_commands=verification_commands_for_roles,
            reproduction=reproduction if isinstance(reproduction, Mapping) else None,
            requires_live=(
                plan.get("requires_live_verification")
                if isinstance(plan.get("requires_live_verification"), bool)
                else None
            ),
        )
    )

    compatibility = plan.get("compatibility_and_failure_modes")
    if not isinstance(compatibility, Mapping):
        reasons.append("change_plan_compatibility_missing")
    else:
        if _string_list(compatibility.get("preserved_behaviors"), nonempty=True) is None:
            reasons.append("change_plan_preserved_behaviors_invalid")
        if _string_list(compatibility.get("intentional_changes")) is None:
            reasons.append("change_plan_intentional_changes_invalid")
        if _string_list(compatibility.get("failure_modes"), nonempty=True) is None:
            reasons.append("change_plan_failure_modes_invalid")
        if not isinstance(compatibility.get("migration_required"), bool):
            reasons.append("change_plan_migration_decision_missing")
    if not isinstance(plan.get("causal_coverage"), Mapping):
        reasons.append("change_plan_causal_coverage_missing")
    if not isinstance(plan.get("requires_live_verification"), bool):
        reasons.append("change_plan_live_verification_decision_missing")

    expected_case_id = _string(problem.get("case_id")) if isinstance(problem, Mapping) else None
    expected_problem_id = (
        _string(problem.get("problem_id")) if isinstance(problem, Mapping) else None
    )
    expected_option_id = (
        _string(selection.get("selected_option_id")) if isinstance(selection, Mapping) else None
    )
    expected_revision = (
        _string(research.get("repo_revision")) if isinstance(research, Mapping) else None
    )
    for field, actual, expected in (
        ("case_id", _string(plan.get("case_id")), expected_case_id),
        ("problem_id", _string(plan.get("problem_id")), expected_problem_id),
        ("selected_option_id", _string(plan.get("selected_option_id")), expected_option_id),
        ("repo_revision", _string(plan.get("repo_revision")), expected_revision),
    ):
        if expected is None or actual != expected:
            reasons.append(f"change_plan_{field}_linkage_mismatch")

    target_contract = plan.get("target_contract")
    if not isinstance(target_contract, Mapping):
        reasons.append("change_plan_target_contract_missing")
    else:
        for field, expected in (
            ("case_id", _string(plan.get("case_id"))),
            ("problem_id", _string(plan.get("problem_id"))),
            ("selected_option_id", _string(plan.get("selected_option_id"))),
            ("repo_revision", _string(plan.get("repo_revision"))),
        ):
            if _string(target_contract.get(field)) != expected:
                reasons.append(f"change_plan_target_contract_{field}_mismatch")
        contract_targets_raw = target_contract.get("targets")
        contract_targets = contract_targets_raw if isinstance(contract_targets_raw, list) else []
        projected_contract_targets = [
            {
                "action": target.get("action"),
                "path": target.get("path"),
                "symbols": target.get("symbols"),
                "change": target.get("change"),
            }
            for target in contract_targets
            if isinstance(target, Mapping)
        ]
        projected_plan_targets = [
            {
                "action": target.get("action"),
                "path": target.get("path"),
                "symbols": target.get("symbols"),
                "change": target.get("change"),
            }
            for target in (targets if isinstance(targets, list) else [])
            if isinstance(target, Mapping)
        ]
        if (
            len(projected_contract_targets) != len(contract_targets)
            or projected_contract_targets != projected_plan_targets
        ):
            reasons.append("change_plan_target_contract_targets_mismatch")

    selected_option = None
    if isinstance(selection, Mapping):
        candidate = selection.get("selected_option")
        selected_option = candidate if isinstance(candidate, Mapping) else None
    if selected_option is not None and plan.get("causal_coverage") != selected_option.get(
        "causal_coverage"
    ):
        reasons.append("change_plan_causal_coverage_linkage_mismatch")
    if selected_option is not None and plan.get("scope_evidence") != selected_option.get(
        "scope_evidence"
    ):
        reasons.append("change_plan_scope_evidence_linkage_mismatch")
    reasons.extend(
        _broad_scope_outcome_coverage_reasons(
            plan,
            selected_option=selected_option,
            research=research,
        )
    )
    if selected_option is not None:
        option_coverage = selected_option.get("causal_coverage")
        option_coverage = option_coverage if isinstance(option_coverage, Mapping) else {}
        binding_raw = option_coverage.get("research_binding")
        binding = binding_raw if isinstance(binding_raw, Mapping) else {}
        points_raw = binding.get("intervention_points")
        points = points_raw if isinstance(points_raw, list) else []
        required_target_interventions = {
            (path, symbol): intervention
            for point in points
            if isinstance(point, Mapping)
            for path in [_string(point.get("target_path"))]
            for symbol in [_string(point.get("target_symbol"))]
            for intervention in [_string(point.get("intervention"))]
            if path is not None and symbol is not None and intervention is not None
        }
        required_target_pairs = set(required_target_interventions)
        actual_target_pairs = {
            (path, symbol)
            for target in (targets if isinstance(targets, list) else [])
            if isinstance(target, Mapping)
            for path in [_string(target.get("path"))]
            for symbol in (_string_list(target.get("symbols")) or [])
            if path is not None
        }
        if not required_target_pairs.issubset(actual_target_pairs):
            reasons.append("change_plan_intervention_targets_missing")
        verified_symbol_paths = _verified_symbol_paths(research)
        verified_evidence = _verified_mechanism_evidence(research)
        verified_evidence_ids = set(verified_evidence)
        for index, target in enumerate(targets if isinstance(targets, list) else []):
            if not isinstance(target, Mapping):
                continue
            path = _string(target.get("path"))
            symbols = _string_list(target.get("symbols")) or []
            extra_pairs = {
                (path, symbol)
                for symbol in symbols
                if path is not None and (path, symbol) not in required_target_pairs
            }
            if not extra_pairs or (path is not None and _verification_only_target(path)):
                continue
            rationale_kind = _string(target.get("rationale_kind"))
            rationale = _string(target.get("rationale"))
            evidence_refs = _string_list(target.get("evidence_refs"), nonempty=True)
            if _string(target.get("action")) == "create":
                if (
                    rationale_kind not in {"causal_propagation", "compatibility"}
                    or rationale is None
                    or evidence_refs is None
                ):
                    reasons.append(f"change_plan_create_target_causal_rationale_missing:{index}")
                reasons.extend(
                    _create_target_integration_reasons(
                        target,
                        index=index,
                        verified_symbol_paths=verified_symbol_paths,
                        verified_evidence=verified_evidence,
                    )
                )
                continue
            if (
                rationale_kind not in {"causal_propagation", "compatibility"}
                or rationale is None
                or evidence_refs is None
                or any(ref not in verified_evidence_ids for ref in evidence_refs)
            ):
                reasons.append(f"change_plan_additional_target_causal_binding_missing:{index}")
        for index, target in enumerate(targets if isinstance(targets, list) else []):
            if not isinstance(target, Mapping):
                continue
            path = _string(target.get("path"))
            change = _string(target.get("change"))
            for symbol in _string_list(target.get("symbols")) or []:
                expected_intervention = required_target_interventions.get((path, symbol))
                if expected_intervention is not None and change != expected_intervention:
                    reasons.append(
                        f"change_plan_intervention_effect_mismatch:{index}:{path}:{symbol}"
                    )

    requires_live, _ = infer_live_verification_requirement(problem, research)
    if plan.get("requires_live_verification") is not requires_live:
        reasons.append("change_plan_live_verification_inference_mismatch")
    return not reasons, list(dict.fromkeys(reasons))


def assess_ticket_readiness(ticket: Mapping[str, Any] | None) -> tuple[bool, list[str]]:
    """Validate the complete stage-backed evidence chain for implementation readiness."""

    if not isinstance(ticket, Mapping):
        return False, ["ticket_missing"]
    reasons: list[str] = []
    problem_raw = ticket.get("problem_record")
    problem = problem_raw if isinstance(problem_raw, Mapping) else None
    priority_raw = ticket.get("priority")
    priority = priority_raw if isinstance(priority_raw, Mapping) else None
    research_raw = ticket.get("research")
    research = research_raw if isinstance(research_raw, Mapping) else None
    selection_raw = ticket.get("selected_solution")
    selection = selection_raw if isinstance(selection_raw, Mapping) else None
    plan_raw = ticket.get("change_plan")
    plan = plan_raw if isinstance(plan_raw, Mapping) else None
    options_raw = ticket.get("solution_options")
    options = (
        [item for item in options_raw if isinstance(item, Mapping)]
        if isinstance(options_raw, list)
        else []
    )

    if problem is None:
        reasons.append("problem_record_missing")
        expected_problem_id = None
        expected_case_id = None
    else:
        expected_problem_id = _string(problem.get("problem_id"))
        expected_case_id = _string(problem.get("case_id"))
        if _string(problem.get("_parse_warning")) is not None:
            reasons.append("problem_record_parse_warning_present")
        if expected_problem_id is None or expected_case_id is None:
            reasons.append("problem_record_lineage_missing")
        canonical_problem_id = _string(problem.get("canonical_problem_id"))
        if canonical_problem_id != expected_problem_id:
            reasons.append("problem_record_canonical_problem_mismatch")
        members = _string_list(problem.get("case_member_problem_ids"), nonempty=True)
        if members is None or expected_problem_id not in members:
            reasons.append("problem_record_case_membership_invalid")

    if priority is None:
        reasons.append("priority_decision_missing")
    else:
        if _string(priority.get("_parse_warning")) is not None:
            reasons.append("priority_decision_parse_warning_present")
        if priority.get("selected_for_research") is not True:
            reasons.append("priority_decision_not_selected_for_research")
        if _string(priority.get("priority_bucket")) is None:
            reasons.append("priority_decision_bucket_missing")
        if _string(priority.get("priority_rationale")) is None:
            reasons.append("priority_decision_rationale_missing")
        if priority.get("priority_status") != "prioritized":
            reasons.append("priority_decision_status_invalid")
        if _string(priority.get("problem_id")) != expected_problem_id:
            reasons.append("priority_decision_problem_mismatch")
        if _string(priority.get("case_id")) != expected_case_id:
            reasons.append("priority_decision_case_mismatch")

    for label, artifact in (
        ("research", research),
        ("selection", selection),
        ("change_plan", plan),
    ):
        if artifact is None:
            continue
        if _string(artifact.get("problem_id")) != expected_problem_id:
            reasons.append(f"{label}_problem_lineage_mismatch")
        if _string(artifact.get("case_id")) != expected_case_id:
            reasons.append(f"{label}_case_lineage_mismatch")
    for index, option in enumerate(options):
        if _string(option.get("problem_id")) != expected_problem_id:
            reasons.append(f"solution_option_problem_lineage_mismatch:{index}")
        if _string(option.get("case_id")) != expected_case_id:
            reasons.append(f"solution_option_case_lineage_mismatch:{index}")

    if research is not None and _string(research.get("_parse_warning")) is not None:
        reasons.append("research_parse_warning_present")

    research_ready, research_reasons = assess_research_readiness(
        dict(research) if research is not None else None
    )
    if not research_ready:
        reasons.extend(research_reasons)
    selection_ready, selection_reasons = assess_selection_readiness(
        selection,
        options=options,
        research=research,
    )
    if not selection_ready:
        reasons.extend(selection_reasons)
    selection_for_plan: Mapping[str, Any] | None = selection
    if selection is not None:
        selection_copy = dict(selection)
        selected_id = _string(selection.get("selected_option_id"))
        selected_option = next(
            (option for option in options if _string(option.get("option_id")) == selected_id),
            None,
        )
        if selected_option is not None:
            selection_copy["selected_option"] = selected_option
        selection_for_plan = selection_copy
    plan_ready, plan_reasons = assess_change_plan_readiness(
        plan,
        problem=problem,
        research=research,
        selection=selection_for_plan,
    )
    if not plan_ready:
        reasons.extend(plan_reasons)
    return not reasons, list(dict.fromkeys(reasons))


__all__ = [
    "assign_plan_revision_id",
    "assess_change_plan_readiness",
    "assess_selection_readiness",
    "assess_solution_option_readiness",
    "assess_ticket_readiness",
    "bind_falsification_review",
    "bind_plan_outcome_oracle",
    "falsification_acceptance_has_adversarial_basis",
    "falsification_review_receipt_errors",
    "infer_live_verification_requirement",
    "plan_revision_id_for",
    "research_evidence_references",
    "research_limitation_references",
    "verified_mechanism_evidence",
    "verified_outcome_oracles",
]
