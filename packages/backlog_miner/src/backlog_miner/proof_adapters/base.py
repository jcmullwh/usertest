"""Adapter protocol and runner-observation helpers for causal proof."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from backlog_core.causal_proof import (
    CAUSAL_PROOF_SCHEMA_VERSION,
    canonical_json_sha256,
    content_bound_payload,
    evaluate_proof_predicate,
    intervention_id_for,
    proof_receipt_id_for,
)

_MISSING = object()


@dataclass(frozen=True)
class ProofAdapterContext:
    case_id: str
    problem_id: str
    hypothesis_id: str
    claim: Mapping[str, Any]
    experiments: Mapping[str, Mapping[str, Any]]
    clean_replays: Mapping[str, Mapping[str, Any]]
    source_root: Mapping[str, Any]
    planning_workspace: Path | None
    atom_bindings: Sequence[Mapping[str, Any]]
    symbol_receipts: Sequence[Mapping[str, Any]]
    artifact_receipts: Sequence[Mapping[str, Any]]
    services: Mapping[str, Callable[..., Any]]


@dataclass(frozen=True)
class ProofAdapterResult:
    receipts: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[str, ...] = ()


class ProofAdapter(Protocol):
    adapter_id: str
    adapter_version: str

    def build(self, context: ProofAdapterContext) -> ProofAdapterResult: ...


def text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def json_pointer_value(value: Any, pointer: str) -> tuple[bool, Any]:
    if pointer == "":
        return True, value
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False, None
    current = value
    for raw_segment in pointer[1:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return False, None
    return True, current


def _stream_text(replay: Mapping[str, Any], stream: str) -> tuple[bool, str]:
    paths: list[Path] = []
    if stream in {"stdout", "stderr"}:
        raw = text(replay.get(f"{stream}_path"))
        paths = [Path(raw)] if raw is not None else []
    elif stream == "combined":
        for name in ("stdout", "stderr"):
            raw = text(replay.get(f"{name}_path"))
            if raw is not None:
                paths.append(Path(raw))
    if not paths or any(not path.is_file() for path in paths):
        return False, ""
    try:
        return True, "".join(path.read_text(encoding="utf-8", errors="replace") for path in paths)
    except OSError:
        return False, ""


def observed_value(
    replay: Mapping[str, Any],
    spec: Any,
) -> tuple[bool, Any, str | None, list[Path]]:
    """Extract only from runner-retained replay fields or artifacts."""
    if not isinstance(spec, Mapping):
        return False, None, None, []
    source = text(spec.get("source"))
    if source == "exit_code":
        value = replay.get("exit_code")
        valid = isinstance(value, int) and not isinstance(value, bool)
        return valid, value, canonical_json_sha256(value) if valid else None, []
    if source == "platform":
        isolation = replay.get("execution_isolation")
        value = isolation.get("platform") if isinstance(isolation, Mapping) else None
        return value is not None, value, canonical_json_sha256(value), []
    if source == "executed_argv":
        argv = replay.get("executed_argv")
        return isinstance(argv, list), argv, canonical_json_sha256(argv), []
    if source in {"stdout_text", "stderr_text", "combined_text", "event_lines"}:
        stream = source.removesuffix("_text").removeprefix("event_")
        stream = "stdout" if source == "event_lines" else stream
        ok, content = _stream_text(replay, stream)
        value: Any = (
            [line for line in content.splitlines() if line]
            if source == "event_lines"
            else content
        )
        paths = [
            Path(path)
            for name in (("stdout",) if source == "event_lines" else (stream,))
            for path in [text(replay.get(f"{name}_path"))]
            if path is not None
        ]
        return ok, value, sha256(content.encode("utf-8")).hexdigest() if ok else None, paths
    if source in {"stdout_json", "stderr_json", "event_json"}:
        stream = "stdout" if source in {"stdout_json", "event_json"} else "stderr"
        ok, content = _stream_text(replay, stream)
        if not ok:
            return False, None, None, []
        try:
            document = json.loads(content)
        except json.JSONDecodeError:
            return False, None, None, []
        pointer = str(spec.get("json_pointer") or "")
        found, value = json_pointer_value(document, pointer)
        path_raw = text(replay.get(f"{stream}_path"))
        paths = [Path(path_raw)] if path_raw is not None else []
        return found, value, canonical_json_sha256(value) if found else None, paths
    return False, None, None, []


def artifact_receipts(paths: Sequence[Path], *, prefix: str) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for index, path in enumerate(dict.fromkeys(path.resolve() for path in paths)):
        if not path.is_file():
            continue
        receipts.append(
            {
                "artifact_id": f"{prefix}:{index}:{sha256(str(path).encode()).hexdigest()[:12]}",
                "path": str(path),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
                "runner_attested": True,
            }
        )
    return receipts


def environment_attestation(
    environment: Mapping[str, str],
    *,
    absent_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Create a value-redacted, content-bound receipt for an actual child environment.

    Runners may retain this under execution metadata.  Values are never exposed to the model or
    proof artifact; the per-variable hashes are sufficient to attest a controlled delta.
    """

    variables = {
        str(key): {
            "present": True,
            "value_sha256": sha256(str(value).encode("utf-8")).hexdigest(),
        }
        for key, value in sorted(environment.items())
        if isinstance(key, str) and key
    }
    variables.update(
        {
            key: {"present": False}
            for key in sorted({key for key in absent_keys if isinstance(key, str) and key})
        }
    )
    return content_bound_payload(
        {
            "runner_attested": True,
            "variables": variables,
        },
        hash_field="environment_attestation_sha256",
    )


def replay_environment_attestation(replay: Mapping[str, Any]) -> Mapping[str, Any] | None:
    direct = replay.get("environment_attestation")
    metadata = replay.get("execution_metadata")
    candidate = (
        direct
        if isinstance(direct, Mapping)
        else metadata.get("environment_attestation")
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(candidate, Mapping) or candidate.get("runner_attested") is not True:
        return None
    expected = content_bound_payload(
        {
            key: value
            for key, value in candidate.items()
            if key != "environment_attestation_sha256"
        },
        hash_field="environment_attestation_sha256",
    )
    if candidate.get("environment_attestation_sha256") != expected.get(
        "environment_attestation_sha256"
    ):
        return None
    variables = candidate.get("variables")
    if not isinstance(variables, Mapping):
        return None
    return candidate


def replay_observation(
    *, experiment_id: str, replay: Mapping[str, Any], observed: Any, observed_sha256: str
) -> dict[str, Any]:
    return content_bound_payload(
        {
            "experiment_id": experiment_id,
            "runner_attested": True,
            "executed_argv": replay.get("executed_argv"),
            "exit_code": replay.get("exit_code"),
            "stdout_sha256": replay.get("stdout_sha256"),
            "stderr_sha256": replay.get("stderr_sha256"),
            "observed": observed,
            "observed_sha256": observed_sha256,
            "execution_isolation_sha256": canonical_json_sha256(
                replay.get("execution_isolation")
            ),
        },
        hash_field="observation_sha256",
    )


def _validated_replay_inputs(
    replay: Mapping[str, Any],
    *,
    experiment_id: str,
) -> dict[str, Any] | None:
    raw = replay.get("replay_inputs")
    if not isinstance(raw, Mapping):
        return None
    projection = {key: value for key, value in raw.items() if key != "replay_inputs_sha256"}
    environment = raw.get("environment")
    paths = raw.get("disposable_state_paths")
    if (
        raw.get("schema_version") != 1
        or raw.get("source_experiment_id") != experiment_id
        or raw.get("runner_approved") is not True
        or raw.get("replay_inputs_sha256") != canonical_json_sha256(projection)
        or not isinstance(environment, Mapping)
        or any(
            not isinstance(key, str)
            or not key
            or (value is not None and not isinstance(value, str))
            for key, value in environment.items()
        )
        or not isinstance(paths, list)
    ):
        return None
    for raw_path in paths:
        if not isinstance(raw_path, str) or not raw_path:
            return None
        posix = PurePosixPath(raw_path.replace("\\", "/"))
        windows = PureWindowsPath(raw_path)
        if posix.is_absolute() or windows.anchor or ".." in posix.parts or ".." in windows.parts:
            return None
    return dict(raw)


def _portable_replay_selector(selector: Any) -> dict[str, Any] | None:
    if not isinstance(selector, Mapping) or text(selector.get("source")) is None:
        return None
    normalized = dict(selector)
    if selector.get("source") == "workspace_state":
        raw_path = text(selector.get("path"))
        if raw_path is None:
            return None
        posix = PurePosixPath(raw_path.replace("\\", "/"))
        windows = PureWindowsPath(raw_path)
        if posix.is_absolute() or windows.anchor or ".." in posix.parts or ".." in windows.parts:
            return None
        normalized["path"] = posix.as_posix()
    return normalized


def runner_node(*, node_id: str, kind: str, locator: str, evidence: Any) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": kind,
        "locator": locator,
        "runner_attested": True,
        "evidence_sha256": canonical_json_sha256(evidence),
    }


def runner_edge(
    *, source: str, target: str, kind: str, evidence: Any
) -> dict[str, Any]:
    return {
        "from_node_id": source,
        "to_node_id": target,
        "kind": kind,
        "runner_attested": True,
        "evidence_sha256": canonical_json_sha256(evidence),
    }


def build_receipt(
    *,
    adapter_id: str,
    adapter_version: str,
    context: ProofAdapterContext,
    baseline_id: str,
    challenge_id: str,
    baseline_observed: Any,
    baseline_observed_sha256: str,
    baseline_selector: Mapping[str, Any],
    challenge_observed: Any,
    challenge_observed_sha256: str,
    challenge_selector: Mapping[str, Any],
    observation_source: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    adapter_evidence: Mapping[str, Any],
    positive_observed: Any = _MISSING,
) -> ProofAdapterResult:
    baseline_replay = context.clean_replays.get(baseline_id)
    challenge_replay = context.clean_replays.get(challenge_id)
    if not isinstance(baseline_replay, Mapping) or not isinstance(challenge_replay, Mapping):
        return ProofAdapterResult(diagnostics=("proof_adapter_replay_missing",))
    replay_inputs = _validated_replay_inputs(
        baseline_replay,
        experiment_id=baseline_id,
    )
    source_selector = _portable_replay_selector(baseline_selector)
    positive_selector = _portable_replay_selector(challenge_selector)
    if replay_inputs is None:
        return ProofAdapterResult(diagnostics=("proof_adapter_replay_inputs_unavailable",))
    if source_selector is None or positive_selector is None:
        return ProofAdapterResult(diagnostics=("proof_adapter_replay_selector_invalid",))
    intervention_raw = context.claim.get("intervention")
    positive_raw = context.claim.get("positive_outcome")
    if not isinstance(intervention_raw, Mapping) or not isinstance(positive_raw, Mapping):
        return ProofAdapterResult(diagnostics=("proof_adapter_claim_incomplete",))
    intervention = {
        "kind": intervention_raw.get("kind"),
        "target": intervention_raw.get("target"),
        "baseline_experiment_id": baseline_id,
        "challenge_experiment_id": challenge_id,
        "predicted_polarity": intervention_raw.get("predicted_polarity"),
        "before": intervention_raw.get("before"),
        "after": intervention_raw.get("after"),
    }
    if baseline_observed == challenge_observed:
        return ProofAdapterResult(diagnostics=("proof_adapter_observations_equivalent",))
    baseline = replay_observation(
        experiment_id=baseline_id,
        replay=baseline_replay,
        observed=baseline_observed,
        observed_sha256=baseline_observed_sha256,
    )
    challenge = replay_observation(
        experiment_id=challenge_id,
        replay=challenge_replay,
        observed=challenge_observed,
        observed_sha256=challenge_observed_sha256,
    )
    predicate = positive_raw.get("predicate")
    evaluated_observed = (
        challenge_observed if positive_observed is _MISSING else positive_observed
    )
    passed, predicate_errors = evaluate_proof_predicate(predicate, evaluated_observed)
    if predicate_errors or not passed:
        return ProofAdapterResult(
            diagnostics=tuple(f"proof_adapter_positive_{error}" for error in predicate_errors)
        )
    root_atoms = [
        atom_id
        for atom_id in context.source_root.get("origin_atom_ids", [])
        if isinstance(atom_id, str) and atom_id
    ]
    positive_basis = context.source_root.get("positive_basis")
    if not isinstance(positive_basis, Mapping):
        return ProofAdapterResult(diagnostics=("proof_adapter_positive_basis_unattested",))
    binding = {
        "origin_atom_ids": root_atoms,
        "basis_kind": positive_basis.get("basis_kind"),
        "basis_sha256": positive_basis.get("basis_sha256"),
    }
    receipt: dict[str, Any] = {
        "schema_version": CAUSAL_PROOF_SCHEMA_VERSION,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "case_id": context.case_id,
        "problem_id": context.problem_id,
        "hypothesis_id": context.hypothesis_id,
        "source_root": dict(context.source_root),
        "observations": {"baseline": baseline, "challenge": challenge},
        "replay_inputs": replay_inputs,
        "replay_observation": content_bound_payload(
            {
                "schema_version": 1,
                "source_experiment_id": baseline_id,
                "selector": source_selector,
                "source_observation_sha256": baseline["observation_sha256"],
                "positive_reference_experiment_id": challenge_id,
                "positive_reference_selector": positive_selector,
                "positive_reference_observation_sha256": challenge[
                    "observation_sha256"
                ],
                "predicate_input_mode": (
                    "historical_baseline_and_post_change_observation"
                    if isinstance(predicate, Mapping)
                    and predicate.get("kind") == "state_transition"
                    else "post_change_observation"
                ),
                "runner_attested": True,
            },
            hash_field="replay_observation_sha256",
        ),
        "intervention": intervention,
        "mechanism_graph": {
            "root_node_id": nodes[0]["node_id"],
            "outcome_node_id": nodes[-1]["node_id"],
            "nodes": nodes,
            "edges": edges,
        },
        "artifacts": artifacts,
        "positive_outcome": {
            "problem_binding": binding,
            "predicate": predicate,
            "observed": evaluated_observed,
            "observation_source": observation_source,
            "runner_evaluated": True,
            "passed": passed,
        },
        "adapter_evidence": dict(adapter_evidence),
    }
    receipt["intervention_id"] = intervention_id_for(
        source_root=receipt["source_root"],
        baseline_observation=baseline,
        challenge_observation=challenge,
        intervention=intervention,
    )
    receipt["proof_receipt_id"] = proof_receipt_id_for(receipt)
    return ProofAdapterResult(receipts=(receipt,))
