"""Pure contracts for runner-attested causal proof adapters.

This module deliberately has no process, filesystem, parser, or adapter dependencies.  It defines
the central invariants every proof method must satisfy while allowing adapter-specific attestation
details under ``adapter_evidence``.  Adapters observe; model-authored claims never mint receipts.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

CAUSAL_PROOF_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
PredicateContractValidator = Callable[[Mapping[str, Any]], list[str]]
PredicateEvaluator = Callable[[Mapping[str, Any], Any], tuple[bool, list[str]]]
_PREDICATE_REGISTRY: dict[
    str,
    tuple[PredicateContractValidator, PredicateEvaluator],
] = {}


def register_proof_predicate(
    kind: str,
    *,
    contract_validator: PredicateContractValidator,
    evaluator: PredicateEvaluator,
    replace: bool = False,
) -> None:
    """Register a deterministic runner predicate without editing the core contract.

    The verifier owns registration. Model output can select a registered predicate,
    but cannot provide executable predicate logic.
    """

    normalized = kind.strip() if isinstance(kind, str) else ""
    if not normalized:
        raise ValueError("predicate_kind_invalid")
    if normalized in _PREDICATE_REGISTRY and not replace:
        raise ValueError(f"predicate_kind_already_registered:{normalized}")
    _PREDICATE_REGISTRY[normalized] = (contract_validator, evaluator)


def canonical_json_sha256(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def content_bound_payload(value: Mapping[str, Any], *, hash_field: str) -> dict[str, Any]:
    payload = {str(key): item for key, item in value.items() if key != hash_field}
    payload[hash_field] = canonical_json_sha256(payload)
    return payload


def command_authorization_errors(
    authorization: Any,
    *,
    argv: Sequence[str],
) -> list[str]:
    """Validate shell-free command authorization from properties and immutable anchors."""

    if not isinstance(authorization, Mapping):
        return ["command_authorization_not_object"]
    errors: list[str] = []
    if (
        not argv
        or any(not isinstance(token, str) or not token for token in argv)
        or not _nonempty_type(authorization.get("authorization_kind"))
        or authorization.get("runner_attested") is not True
        or authorization.get("executed_argv_sha256") != canonical_json_sha256(list(argv))
        or authorization.get("shell") is not False
        or authorization.get("workspace_confined") is not True
        or authorization.get("authorization_sha256")
        != canonical_json_sha256(
            {
                key: value
                for key, value in authorization.items()
                if key != "authorization_sha256"
            }
        )
    ):
        errors.append("command_authorization_attestation_invalid")

    def digest(value: Any) -> bool:
        return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None

    def git_digest(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40,64}", value) is not None

    repository_bindings = authorization.get("repository_bindings")
    repository_bound = bool(
        isinstance(repository_bindings, list)
        and repository_bindings
        and all(
            isinstance(binding, Mapping)
            and _nonempty_type(binding.get("path"))
            and _nonempty_type(binding.get("relationship"))
            and binding.get("runner_attested") is True
            and digest(binding.get("file_sha256"))
            and git_digest(binding.get("git_blob_sha"))
            and binding.get("repository_binding_sha256")
            == canonical_json_sha256(
                {
                    key: value
                    for key, value in binding.items()
                    if key != "repository_binding_sha256"
                }
            )
            for binding in repository_bindings
        )
    )
    origin_bound = digest(authorization.get("origin_atom_sha256")) and digest(
        authorization.get("origin_command_value_sha256")
    )
    entrypoint_bound = digest(authorization.get("entrypoint_sha256")) and (
        git_digest(authorization.get("entrypoint_git_blob_sha"))
        or digest(authorization.get("declaration_sha256"))
        or _nonempty_type(authorization.get("artifact_id"))
    )
    if not (repository_bound or origin_bound or entrypoint_bound):
        errors.append("command_authorization_immutable_anchor_missing")
    return list(dict.fromkeys(errors))


def command_authorization_identity(authorization: Any) -> dict[str, Any] | None:
    """Project the immutable repository/source identity independently of argv arguments."""

    if not isinstance(authorization, Mapping):
        return None
    repository_bindings = authorization.get("repository_bindings")
    if isinstance(repository_bindings, list) and repository_bindings:
        return {
            "identity_kind": "repository_bindings",
            "repository_binding_sha256s": sorted(
                str(binding.get("repository_binding_sha256"))
                for binding in repository_bindings
                if isinstance(binding, Mapping)
            ),
        }
    if isinstance(authorization.get("entrypoint_sha256"), str):
        return {
            "identity_kind": "repository_entrypoint",
            "entrypoint_path": authorization.get("entrypoint_path"),
            "entrypoint_sha256": authorization.get("entrypoint_sha256"),
            "entrypoint_git_blob_sha": authorization.get("entrypoint_git_blob_sha"),
            "declaration_sha256": authorization.get("declaration_sha256"),
            "artifact_id": authorization.get("artifact_id"),
        }
    if isinstance(authorization.get("origin_atom_sha256"), str):
        return {
            "identity_kind": "origin_command",
            "origin_atom_id": authorization.get("origin_atom_id"),
            "origin_atom_sha256": authorization.get("origin_atom_sha256"),
            "origin_atom_field_path": authorization.get("origin_atom_field_path"),
            "origin_command_value_sha256": authorization.get(
                "origin_command_value_sha256"
            ),
        }
    return None


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _nonempty_type(value: Any) -> bool:
    """Return whether an adapter-owned semantic type is explicit and nonempty.

    Adapter identifiers, intervention polarities, mechanism node/edge kinds, and problem-binding
    basis kinds are intentionally open vocabularies.  Central validation checks that an adapter
    typed the value; adapter registration and conformance own the meaning of that type.
    """

    return isinstance(value, str) and bool(value.strip())


def _schema_errors(value: Any, schema: Any, *, path: str = "$") -> list[str]:
    if not isinstance(schema, Mapping):
        return [f"predicate_schema_invalid:{path}"]
    errors: list[str] = []
    expected_type = schema.get("type")
    type_checks = {
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": _finite_number,
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected_type is not None:
        check = type_checks.get(str(expected_type))
        if check is None:
            return [f"predicate_schema_type_unsupported:{path}:{expected_type}"]
        if not check(value):
            return [f"predicate_schema_type_mismatch:{path}:{expected_type}"]
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            errors.append(f"predicate_schema_required_invalid:{path}")
        else:
            errors.extend(
                f"predicate_schema_required_missing:{path}.{key}"
                for key in required
                if key not in value
            )
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            errors.append(f"predicate_schema_properties_invalid:{path}")
        else:
            for key, child_schema in properties.items():
                if isinstance(key, str) and key in value:
                    errors.extend(_schema_errors(value[key], child_schema, path=f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        errors.extend(
            error
            for index, item in enumerate(value)
            for error in _schema_errors(item, schema["items"], path=f"{path}[{index}]")
        )
    return errors


def evaluate_proof_predicate(predicate: Any, observed: Any) -> tuple[bool, list[str]]:
    """Evaluate one generic predicate against runner-observed data."""
    if not isinstance(predicate, Mapping):
        return False, ["predicate_not_object"]
    kind = predicate.get("kind")
    registered = _PREDICATE_REGISTRY.get(str(kind))
    if registered is None:
        return False, [f"predicate_kind_unsupported:{kind}"]
    contract_errors = registered[0](predicate)
    if contract_errors:
        return False, contract_errors
    passed, errors = registered[1](predicate, observed)
    if errors:
        return False, errors
    return passed, ([] if passed else [f"predicate_not_satisfied:{kind}"])


def proof_predicate_contract_errors(predicate: Any) -> list[str]:
    """Validate generic predicate syntax without requiring an observation."""
    if not isinstance(predicate, Mapping):
        return ["predicate_not_object"]
    kind = predicate.get("kind")
    registered = _PREDICATE_REGISTRY.get(str(kind))
    if registered is None:
        return [f"predicate_kind_unsupported:{kind}"]
    return registered[0](predicate)


def _builtin_predicate_contract(predicate: Mapping[str, Any]) -> list[str]:
    kind = predicate.get("kind")
    if kind == "equals" and "expected" not in predicate:
        return ["predicate_expected_missing"]
    if kind == "membership" and not isinstance(predicate.get("members"), list):
        return ["predicate_members_invalid"]
    if kind == "contains" and (
        not isinstance(predicate.get("expected"), str)
        or not str(predicate.get("expected")).strip()
    ):
        return ["predicate_contains_expected_invalid"]
    if kind == "range" and predicate.get("minimum") is None and predicate.get("maximum") is None:
        return ["predicate_range_unbounded"]
    if kind == "schema" and not isinstance(predicate.get("schema"), Mapping):
        return ["predicate_schema_invalid:$"]
    if kind == "existence" and not isinstance(predicate.get("expected"), bool):
        return ["predicate_existence_expected_invalid"]
    if kind == "state_transition" and ("from" not in predicate or "to" not in predicate):
        return ["predicate_state_transition_expected_invalid"]
    if kind == "event_sequence" and (
        not isinstance(predicate.get("events"), list)
        or not predicate.get("events")
        or predicate.get("mode", "exact") not in {"exact", "ordered_subsequence"}
    ):
        return ["predicate_event_sequence_invalid"]
    return []


def _evaluate_builtin_predicate(
    predicate: Mapping[str, Any], observed: Any
) -> tuple[bool, list[str]]:
    kind = predicate.get("kind")
    if kind == "equals":
        return observed == predicate.get("expected"), []
    if kind == "membership":
        return observed in predicate.get("members", []), []
    if kind == "contains":
        if not isinstance(observed, str):
            return False, ["predicate_contains_observation_invalid"]
        return str(predicate.get("expected")) in observed, []
    if kind == "range":
        minimum = predicate.get("minimum")
        maximum = predicate.get("maximum")
        if not _finite_number(observed) or (
            minimum is not None and not _finite_number(minimum)
        ) or (maximum is not None and not _finite_number(maximum)):
            return False, ["predicate_range_values_invalid"]
        passed = True
        if minimum is not None:
            passed = passed and (
                observed >= minimum
                if predicate.get("minimum_inclusive", True) is True
                else observed > minimum
            )
        if maximum is not None:
            passed = passed and (
                observed <= maximum
                if predicate.get("maximum_inclusive", True) is True
                else observed < maximum
            )
        return passed, []
    if kind == "schema":
        errors = _schema_errors(observed, predicate.get("schema"))
        return not errors, errors
    if kind == "existence":
        if not isinstance(observed, Mapping) or not isinstance(observed.get("exists"), bool):
            return False, ["predicate_existence_observation_invalid"]
        return observed.get("exists") is predicate.get("expected"), []
    if kind == "state_transition":
        if not isinstance(observed, Mapping) or "before" not in observed or "after" not in observed:
            return False, ["predicate_state_transition_observation_invalid"]
        return (
            observed.get("before") == predicate.get("from")
            and observed.get("after") == predicate.get("to")
        ), []
    if kind == "event_sequence":
        expected = predicate.get("events")
        if not isinstance(observed, list):
            return False, ["predicate_event_sequence_observation_invalid"]
        if predicate.get("mode", "exact") == "exact":
            return observed == expected, []
        iterator = iter(observed)
        return all(any(candidate == event for candidate in iterator) for event in expected), []
    return False, [f"predicate_kind_unsupported:{kind}"]


for _builtin_predicate_kind in (
    "equals",
    "membership",
    "contains",
    "range",
    "schema",
    "existence",
    "state_transition",
    "event_sequence",
):
    register_proof_predicate(
        _builtin_predicate_kind,
        contract_validator=_builtin_predicate_contract,
        evaluator=_evaluate_builtin_predicate,
    )


def intervention_id_for(
    *,
    source_root: Mapping[str, Any],
    baseline_observation: Mapping[str, Any],
    challenge_observation: Mapping[str, Any],
    intervention: Mapping[str, Any],
) -> str:
    projection = {
        "source_root_sha256": source_root.get("source_root_sha256"),
        "baseline_observation_sha256": baseline_observation.get("observation_sha256"),
        "challenge_observation_sha256": challenge_observation.get("observation_sha256"),
        "intervention": {
            key: value
            for key, value in intervention.items()
            if key not in {"intervention_id", "adapter_evidence"}
        },
    }
    return "intervention:" + canonical_json_sha256(projection)


def proof_receipt_id_for(receipt: Mapping[str, Any]) -> str:
    projection = {key: value for key, value in receipt.items() if key != "proof_receipt_id"}
    return "causal_proof:" + canonical_json_sha256(projection)


def _hash_bound_errors(value: Any, *, hash_field: str, label: str) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{label}_not_object"]
    supplied = value.get(hash_field)
    projection = {key: item for key, item in value.items() if key != hash_field}
    if not isinstance(supplied, str) or supplied != canonical_json_sha256(projection):
        return [f"{label}_hash_invalid"]
    return []


def _portable_selector_errors(selector: Any, *, label: str) -> list[str]:
    if not isinstance(selector, Mapping) or not _nonempty_type(selector.get("source")):
        return [f"{label}_invalid"]
    if selector.get("source") != "workspace_state":
        return []
    path = selector.get("path")
    if not isinstance(path, str) or not path:
        return [f"{label}_workspace_path_invalid"]
    posix = PurePosixPath(path.replace("\\", "/"))
    windows = PureWindowsPath(path)
    if posix.is_absolute() or windows.anchor or ".." in posix.parts or ".." in windows.parts:
        return [f"{label}_workspace_path_invalid"]
    return []


def validate_causal_proof_receipt(receipt: Any) -> list[str]:
    """Validate adapter-independent invariants for one runner-minted proof receipt."""
    if not isinstance(receipt, Mapping):
        return ["causal_proof_not_object"]
    errors: list[str] = []
    if receipt.get("schema_version") != CAUSAL_PROOF_SCHEMA_VERSION:
        errors.append("causal_proof_schema_version_invalid")
    for field in ("adapter_id", "adapter_version", "case_id", "problem_id", "hypothesis_id"):
        if not isinstance(receipt.get(field), str) or not str(receipt[field]).strip():
            errors.append(f"causal_proof_{field}_invalid")

    source_root = receipt.get("source_root")
    errors.extend(
        _hash_bound_errors(
            source_root,
            hash_field="source_root_sha256",
            label="causal_proof_source_root",
        )
    )
    source_atom_ids: list[str] = []
    positive_basis: Mapping[str, Any] | None = None
    if isinstance(source_root, Mapping):
        source_atom_ids = [
            atom_id
            for atom_id in source_root.get("origin_atom_ids", [])
            if isinstance(atom_id, str) and atom_id
        ]
        if not source_atom_ids:
            errors.append("causal_proof_source_root_atoms_missing")
        if not _nonempty_type(source_root.get("root_kind")):
            errors.append("causal_proof_source_root_kind_invalid")
        if not isinstance(source_root.get("runner_attested"), bool) or not source_root.get(
            "runner_attested"
        ):
            errors.append("causal_proof_source_root_not_attested")
        positive_basis_raw = source_root.get("positive_basis")
        positive_basis = (
            positive_basis_raw if isinstance(positive_basis_raw, Mapping) else None
        )
        errors.extend(
            _hash_bound_errors(
                positive_basis,
                hash_field="basis_sha256",
                label="causal_proof_positive_basis",
            )
        )
        if isinstance(positive_basis, Mapping):
            if positive_basis.get("runner_attested") is not True:
                errors.append("causal_proof_positive_basis_not_attested")
            if not _nonempty_type(positive_basis.get("basis_kind")):
                errors.append("causal_proof_positive_basis_kind_invalid")

    observations = receipt.get("observations")
    baseline = observations.get("baseline") if isinstance(observations, Mapping) else None
    challenge = observations.get("challenge") if isinstance(observations, Mapping) else None
    for label, observation in (("baseline", baseline), ("challenge", challenge)):
        errors.extend(
            _hash_bound_errors(
                observation,
                hash_field="observation_sha256",
                label=f"causal_proof_{label}_observation",
            )
        )
        if isinstance(observation, Mapping):
            if observation.get("runner_attested") is not True:
                errors.append(f"causal_proof_{label}_not_runner_attested")
            if not isinstance(observation.get("experiment_id"), str):
                errors.append(f"causal_proof_{label}_experiment_missing")
    if isinstance(baseline, Mapping) and isinstance(challenge, Mapping):
        if baseline.get("experiment_id") == challenge.get("experiment_id"):
            errors.append("causal_proof_observations_not_distinct")
        if baseline.get("observation_sha256") == challenge.get("observation_sha256"):
            errors.append("causal_proof_observations_equivalent")

    replay_inputs = receipt.get("replay_inputs")
    if replay_inputs is not None:
        errors.extend(
            _hash_bound_errors(
                replay_inputs,
                hash_field="replay_inputs_sha256",
                label="causal_proof_replay_inputs",
            )
        )
        if isinstance(replay_inputs, Mapping):
            environment = replay_inputs.get("environment")
            paths = replay_inputs.get("disposable_state_paths")
            if (
                replay_inputs.get("schema_version") != 1
                or replay_inputs.get("runner_approved") is not True
                or not isinstance(environment, Mapping)
                or any(
                    not isinstance(key, str)
                    or not key
                    or (value is not None and not isinstance(value, str))
                    for key, value in environment.items()
                )
                or not isinstance(paths, list)
            ):
                errors.append("causal_proof_replay_inputs_invalid")
            if isinstance(baseline, Mapping) and replay_inputs.get(
                "source_experiment_id"
            ) != baseline.get("experiment_id"):
                errors.append("causal_proof_replay_inputs_source_mismatch")
            for path in paths if isinstance(paths, list) else []:
                if not isinstance(path, str) or not path:
                    errors.append("causal_proof_replay_input_path_invalid")
                    continue
                posix = PurePosixPath(path.replace("\\", "/"))
                windows = PureWindowsPath(path)
                if (
                    posix.is_absolute()
                    or windows.anchor
                    or ".." in posix.parts
                    or ".." in windows.parts
                ):
                    errors.append("causal_proof_replay_input_path_invalid")

    replay_observation = receipt.get("replay_observation")
    if replay_observation is not None:
        errors.extend(
            _hash_bound_errors(
                replay_observation,
                hash_field="replay_observation_sha256",
                label="causal_proof_replay_observation",
            )
        )
        if isinstance(replay_observation, Mapping):
            if (
                replay_observation.get("schema_version") != 1
                or replay_observation.get("runner_attested") is not True
                or replay_observation.get("predicate_input_mode")
                not in {
                    "post_change_observation",
                    "historical_baseline_and_post_change_observation",
                }
            ):
                errors.append("causal_proof_replay_observation_invalid")
            errors.extend(
                _portable_selector_errors(
                    replay_observation.get("selector"),
                    label="causal_proof_replay_selector",
                )
            )
            errors.extend(
                _portable_selector_errors(
                    replay_observation.get("positive_reference_selector"),
                    label="causal_proof_positive_reference_selector",
                )
            )
            if isinstance(baseline, Mapping) and (
                replay_observation.get("source_experiment_id")
                != baseline.get("experiment_id")
                or replay_observation.get("source_observation_sha256")
                != baseline.get("observation_sha256")
            ):
                errors.append("causal_proof_replay_observation_source_mismatch")
            if isinstance(challenge, Mapping) and (
                replay_observation.get("positive_reference_experiment_id")
                != challenge.get("experiment_id")
                or replay_observation.get("positive_reference_observation_sha256")
                != challenge.get("observation_sha256")
            ):
                errors.append("causal_proof_replay_observation_reference_mismatch")

    if isinstance(source_root, Mapping) and isinstance(baseline, Mapping):
        predicate_bindings = source_root.get("atom_field_predicate_bindings")
        if predicate_bindings is not None and not isinstance(predicate_bindings, list):
            errors.append("causal_proof_atom_predicate_bindings_invalid")
        for binding in predicate_bindings if isinstance(predicate_bindings, list) else []:
            if not isinstance(binding, Mapping):
                errors.append("causal_proof_atom_predicate_binding_invalid")
                continue
            errors.extend(
                _hash_bound_errors(
                    binding,
                    hash_field="atom_field_binding_sha256",
                    label="causal_proof_atom_predicate_binding",
                )
            )
            predicate = binding.get("observation_predicate")
            atom_value = binding.get("origin_atom_value")
            atom_passed, atom_errors = evaluate_proof_predicate(predicate, atom_value)
            baseline_passed, baseline_errors = evaluate_proof_predicate(
                predicate,
                baseline.get("observed"),
            )
            errors.extend(
                f"causal_proof_atom_predicate_{error}"
                for error in (*atom_errors, *baseline_errors)
            )
            if (
                binding.get("runner_attested") is not True
                or binding.get("atom_id") not in source_atom_ids
                or not _nonempty_type(binding.get("origin_atom_field_path"))
                or binding.get("origin_atom_value_sha256")
                != canonical_json_sha256(atom_value)
                or binding.get("observation_predicate_sha256")
                != canonical_json_sha256(predicate)
                or binding.get("baseline_experiment_id")
                != baseline.get("experiment_id")
                or binding.get("baseline_observation_sha256")
                != baseline.get("observation_sha256")
                or binding.get("adapter_id") != receipt.get("adapter_id")
                or binding.get("adapter_version") != receipt.get("adapter_version")
                or not atom_passed
                or not baseline_passed
            ):
                errors.append("causal_proof_atom_predicate_binding_invalid")

    intervention = receipt.get("intervention")
    if not isinstance(intervention, Mapping):
        errors.append("causal_proof_intervention_not_object")
    else:
        for field in ("kind", "target", "baseline_experiment_id", "challenge_experiment_id"):
            if not isinstance(intervention.get(field), str) or not str(intervention[field]).strip():
                errors.append(f"causal_proof_intervention_{field}_invalid")
        if not _nonempty_type(intervention.get("predicted_polarity")):
            errors.append("causal_proof_intervention_polarity_invalid")
        if isinstance(baseline, Mapping) and intervention.get(
            "baseline_experiment_id"
        ) != baseline.get("experiment_id"):
            errors.append("causal_proof_intervention_baseline_mismatch")
        if isinstance(challenge, Mapping) and intervention.get(
            "challenge_experiment_id"
        ) != challenge.get("experiment_id"):
            errors.append("causal_proof_intervention_challenge_mismatch")

    graph = receipt.get("mechanism_graph")
    nodes_raw = graph.get("nodes") if isinstance(graph, Mapping) else None
    edges_raw = graph.get("edges") if isinstance(graph, Mapping) else None
    nodes = nodes_raw if isinstance(nodes_raw, list) else []
    edges = edges_raw if isinstance(edges_raw, list) else []
    node_ids: set[str] = set()
    adjacency: dict[str, set[str]] = {}
    for node in nodes:
        if not isinstance(node, Mapping):
            errors.append("causal_proof_mechanism_node_invalid")
            continue
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id or node_id in node_ids:
            errors.append("causal_proof_mechanism_node_id_invalid")
            continue
        node_ids.add(node_id)
        if not _nonempty_type(node.get("kind")):
            errors.append(f"causal_proof_mechanism_node_kind_invalid:{node_id}")
        if node.get("runner_attested") is not True or not isinstance(
            node.get("evidence_sha256"), str
        ) or _SHA256_RE.fullmatch(str(node.get("evidence_sha256"))) is None:
            errors.append(f"causal_proof_mechanism_node_not_attested:{node_id}")
    for edge in edges:
        if not isinstance(edge, Mapping):
            errors.append("causal_proof_mechanism_edge_invalid")
            continue
        source = edge.get("from_node_id")
        target = edge.get("to_node_id")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source == target
            or source not in node_ids
            or target not in node_ids
            or not _nonempty_type(edge.get("kind"))
            or edge.get("runner_attested") is not True
            or not isinstance(edge.get("evidence_sha256"), str)
            or _SHA256_RE.fullmatch(str(edge.get("evidence_sha256"))) is None
        ):
            errors.append("causal_proof_mechanism_edge_not_attested")
            continue
        adjacency.setdefault(source, set()).add(target)
    if not nodes or not edges:
        errors.append("causal_proof_mechanism_graph_empty")
    root_node = graph.get("root_node_id") if isinstance(graph, Mapping) else None
    outcome_node = graph.get("outcome_node_id") if isinstance(graph, Mapping) else None
    if root_node not in node_ids or outcome_node not in node_ids:
        errors.append("causal_proof_mechanism_graph_endpoints_invalid")
    else:
        reachable = {str(root_node)}
        frontier = [str(root_node)]
        while frontier:
            current = frontier.pop()
            for target in adjacency.get(current, set()):
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        if outcome_node not in reachable:
            errors.append("causal_proof_mechanism_graph_disconnected")

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("causal_proof_artifacts_missing")
    else:
        for artifact in artifacts:
            if (
                not isinstance(artifact, Mapping)
                or not isinstance(artifact.get("artifact_id"), str)
                or not isinstance(artifact.get("sha256"), str)
                or _SHA256_RE.fullmatch(str(artifact.get("sha256"))) is None
                or not isinstance(artifact.get("size_bytes"), int)
                or isinstance(artifact.get("size_bytes"), bool)
                or artifact.get("size_bytes") < 0
                or artifact.get("runner_attested") is not True
            ):
                errors.append("causal_proof_artifact_invalid")

    positive = receipt.get("positive_outcome")
    if not isinstance(positive, Mapping):
        errors.append("causal_proof_positive_outcome_missing")
    else:
        contract_role = positive.get("contract_role")
        if contract_role is not None and contract_role != "causal_contrast":
            errors.append("causal_proof_positive_contract_role_invalid")
        binding = positive.get("problem_binding")
        bound_atoms = (
            {
                atom_id
                for atom_id in binding.get("origin_atom_ids", [])
                if isinstance(atom_id, str) and atom_id
            }
            if isinstance(binding, Mapping)
            else set()
        )
        if (
            not bound_atoms.intersection(source_atom_ids)
            or not isinstance(binding, Mapping)
            or not _nonempty_type(binding.get("basis_kind"))
            or not isinstance(binding.get("basis_sha256"), str)
            or _SHA256_RE.fullmatch(str(binding.get("basis_sha256"))) is None
        ):
            errors.append("causal_proof_positive_outcome_not_problem_bound")
        if isinstance(binding, Mapping) and isinstance(positive_basis, Mapping) and (
            binding.get("basis_kind") != positive_basis.get("basis_kind")
            or binding.get("basis_sha256") != positive_basis.get("basis_sha256")
        ):
            errors.append("causal_proof_positive_outcome_basis_mismatch")
        predicate = positive.get("predicate")
        if isinstance(replay_observation, Mapping):
            expected_input_mode = (
                "historical_baseline_and_post_change_observation"
                if isinstance(predicate, Mapping)
                and predicate.get("kind") == "state_transition"
                else "post_change_observation"
            )
            if replay_observation.get("predicate_input_mode") != expected_input_mode:
                errors.append("causal_proof_replay_observation_mode_mismatch")
        if isinstance(positive_basis, Mapping) and positive_basis.get(
            "predicate_sha256"
        ) != canonical_json_sha256(predicate):
            errors.append("causal_proof_positive_basis_predicate_mismatch")
        passed, predicate_errors = evaluate_proof_predicate(predicate, positive.get("observed"))
        errors.extend(f"causal_proof_{error}" for error in predicate_errors)
        if positive.get("runner_evaluated") is not True or positive.get("passed") is not passed:
            errors.append("causal_proof_positive_outcome_evaluation_mismatch")
        if not _nonempty_type(positive.get("observation_source")):
            errors.append("causal_proof_positive_outcome_source_invalid")
        if isinstance(baseline, Mapping) and isinstance(challenge, Mapping):
            before = baseline.get("observed")
            after = challenge.get("observed")
            if before == after:
                errors.append("causal_proof_observed_delta_missing")
            positive_observed = positive.get("observed")
            direct_binding = positive_observed == after
            transition_binding = (
                isinstance(positive_observed, Mapping)
                and positive_observed.get("before") == before
                and positive_observed.get("after") == after
            )
            if not direct_binding and not transition_binding:
                errors.append("causal_proof_positive_outcome_not_challenge_bound")
            if direct_binding:
                baseline_passed, baseline_predicate_errors = evaluate_proof_predicate(
                    predicate,
                    before,
                )
                if baseline_passed and not baseline_predicate_errors:
                    errors.append(
                        "causal_proof_baseline_already_satisfies_positive_outcome"
                    )

    if isinstance(source_root, Mapping) and isinstance(baseline, Mapping) and isinstance(
        challenge, Mapping
    ) and isinstance(intervention, Mapping):
        expected_id = intervention_id_for(
            source_root=source_root,
            baseline_observation=baseline,
            challenge_observation=challenge,
            intervention=intervention,
        )
        if receipt.get("intervention_id") != expected_id:
            errors.append("causal_proof_intervention_id_invalid")
    if receipt.get("proof_receipt_id") != proof_receipt_id_for(receipt):
        errors.append("causal_proof_receipt_id_invalid")
    return list(dict.fromkeys(errors))


def material_unknowns_block_advancement(material_unknowns: Any) -> bool:
    if not isinstance(material_unknowns, Sequence) or isinstance(material_unknowns, (str, bytes)):
        return True
    for item in material_unknowns:
        if not isinstance(item, Mapping):
            return True
        # The field is named material_unknowns, so legacy entries are material by default.
        # New schemas can explicitly retain a useful non-blocking uncertainty with material=false.
        if item.get("material") is not False:
            return True
    return False
